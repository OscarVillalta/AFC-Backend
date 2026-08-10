"""QuickBooks order creation service — shared by API route and bulk import script."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import requests
from sqlalchemy import func, or_, select
from sqlalchemy.exc import DatabaseError, IntegrityError

from app.api.error_handling import safe_commit
from app.api.qb_xml_parser import extract_qb_metadata, parse_qb_line_items
from app.api.validation import ValidationError as CustomValidationError
from app.api.error_handling import (
    DuplicateResourceError,
    ExternalServiceError,
)
from app.config import Config
from database.models import (
    AirFilter,
    BlockedItem,
    Customer,
    Department,
    Media,
    Order,
    OrderItem,
    OrderItemType,
    OrderStatus,
    OrderTracker,
    OrderType,
    Product,
    Quantity,
    StockItem,
    StockItemCategory,
    Supplier,
    Warehouse,
)

VALID_QB_DOC_TYPES = [
    "sales_order",
    "salesorder",
    "estimate",
    "invoice",
    "purchase_order",
    "purchaseorder",
]

NORMALIZED_QB_DOC_TYPES = {
    "sales_order",
    "estimate",
    "invoice",
    "purchase_order",
}

INVOICE_QB_NUMBER_THRESHOLD = 2000


@dataclass
class QBQueryResult:
    success: bool
    qbxml_response: str = ""
    error_message: str | None = None
    error_code: str | None = None


@dataclass
class CreateOrderFromQBResult:
    order: Order
    customer: Customer | None = None
    supplier: Supplier | None = None
    created_items: list[dict[str, Any]] = field(default_factory=list)
    new_products: list[dict[str, Any]] = field(default_factory=list)
    skipped_items: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def normalize_qb_doc_type(qb_doc_type: str) -> str:
    return qb_doc_type.replace("salesorder", "sales_order").replace("purchaseorder", "purchase_order")


def infer_qb_doc_type_for_order(
    order_type: str,
    external_order_number: str | None,
) -> str | None:
    """Infer QB doc type for legacy orders without an explicit qb_doc_type."""
    if not external_order_number or not str(external_order_number).strip():
        return None
    if order_type == OrderType.VOID.value:
        return None
    if order_type == OrderType.INCOMING.value:
        return "purchase_order"
    ref = str(external_order_number).strip()
    try:
        if int(ref) > INVOICE_QB_NUMBER_THRESHOLD:
            return "invoice"
    except ValueError:
        pass
    return "sales_order"


def find_order_by_external_ref(
    db,
    external_number: str,
    qb_doc_type: str,
) -> Order | None:
    normalized = normalize_qb_doc_type(qb_doc_type)
    ref = external_number.strip()
    return db.execute(
        select(Order).where(
            Order.external_order_number == ref,
            Order.qb_doc_type == normalized,
        )
    ).scalar_one_or_none()


def find_orders_by_external_ref(db, external_number: str) -> list[Order]:
    ref = external_number.strip()
    return list(
        db.scalars(
            select(Order)
            .where(Order.external_order_number == ref)
            .order_by(Order.id)
        ).all()
    )


def assert_external_ref_available(
    db,
    external_number: str,
    qb_doc_type: str,
    *,
    exclude_order_id: int | None = None,
) -> None:
    existing = find_order_by_external_ref(db, external_number, qb_doc_type)
    if existing and existing.id != exclude_order_id:
        normalized = normalize_qb_doc_type(qb_doc_type)
        raise DuplicateResourceError(
            "Order",
            "external_order_number and qb_doc_type",
            f"{external_number.strip()} ({normalized})",
        )


def qb_doc_type_from_slip(slip: str | int) -> str:
    """Slip at or below 2000 is a sales_order; above 2000 is an invoice."""
    return "sales_order" if int(slip) <= INVOICE_QB_NUMBER_THRESHOLD else "invoice"


def validate_qb_doc_type(qb_doc_type: str) -> str:
    lowered = qb_doc_type.lower()
    if lowered not in VALID_QB_DOC_TYPES:
        raise CustomValidationError(
            f"qb_doc_type must be one of: {', '.join(VALID_QB_DOC_TYPES)}"
        )
    return normalize_qb_doc_type(lowered)


def query_qb_document(reference_number: str, qb_doc_type: str) -> QBQueryResult:
    entity_type = validate_qb_doc_type(qb_doc_type)
    headers: dict[str, str] = {}
    if Config.QB_API_KEY:
        headers["X-API-Key"] = Config.QB_API_KEY

    try:
        response = requests.post(
            f"{Config.QB_AGENT_URL}/jobs",
            json={
                "op": "query",
                "entity": entity_type,
                "params": {"refnumber": reference_number},
            },
            headers=headers,
            timeout=Config.QB_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        qb_result = response.json()
    except requests.exceptions.Timeout:
        raise ExternalServiceError(
            "QuickBooks Agent",
            f"Request timed out after {Config.QB_REQUEST_TIMEOUT} seconds",
        )
    except requests.exceptions.ConnectionError:
        raise ExternalServiceError(
            "QuickBooks Agent",
            "Connection refused. Is the QB Agent running?",
        )
    except requests.RequestException as e:
        raise ExternalServiceError("QuickBooks Agent", str(e))

    if not qb_result.get("success"):
        return QBQueryResult(
            success=False,
            error_message=qb_result.get("errorMessage"),
            error_code=qb_result.get("errorCode"),
        )

    return QBQueryResult(
        success=True,
        qbxml_response=qb_result.get("qbxmlResponse", ""),
    )


def fetch_qb_order_data(reference_number: str, qb_doc_type: str) -> tuple[list[dict], dict, str]:
    """Query QB and parse line items. Raises ValueError if not found or empty."""
    query = query_qb_document(reference_number, qb_doc_type)
    if not query.success:
        raise ValueError(query.error_message or "QuickBooks query failed")

    entity_type = validate_qb_doc_type(qb_doc_type)
    line_items = parse_qb_line_items(query.qbxml_response, entity_type)
    metadata = extract_qb_metadata(query.qbxml_response, entity_type)

    if not line_items:
        raise ValueError("No line items found in QuickBooks response")

    return line_items, metadata, entity_type


def find_product_by_name(db, item_name: str):
    if not item_name:
        return None

    air_filter = db.execute(
        select(AirFilter).where(
            or_(
                AirFilter.part_number == item_name,
                func.lower(AirFilter.part_number) == item_name.lower(),
            )
        )
    ).first()

    if air_filter:
        if air_filter[0].product:
            return air_filter[0].product
        if getattr(air_filter[0], "child_product", None):
            return air_filter[0].child_product.parent_product

    media_item = db.execute(
        select(Media).where(
            or_(
                Media.part_number == item_name,
                func.lower(Media.part_number) == item_name.lower(),
            )
        )
    ).first()

    if media_item and getattr(media_item[0], "product", None):
        return media_item[0].product

    stock_item = db.execute(
        select(StockItem).where(
            or_(
                StockItem.name == item_name,
                func.lower(StockItem.name) == item_name.lower(),
            )
        )
    ).first()

    if stock_item:
        if stock_item[0].product:
            return stock_item[0].product
        if getattr(stock_item[0], "child_product", None):
            return stock_item[0].child_product.parent_product

    return None


def create_order_from_qb_record(
    db,
    *,
    reference_number: str,
    qb_doc_type: str,
    order_type: str | None = None,
    warehouse_id: int,
    description_override: str | None = None,
) -> CreateOrderFromQBResult:
    reference_number = reference_number.strip()
    normalized_doc_type = validate_qb_doc_type(qb_doc_type)

    assert_external_ref_available(db, reference_number, normalized_doc_type)

    line_items, metadata, entity_type = fetch_qb_order_data(reference_number, normalized_doc_type)
    is_purchase_order = entity_type in ("purchase_order", "purchaseorder")

    customer = None
    supplier = None

    if is_purchase_order:
        vendor_name = metadata.get("vendor_name")
        if vendor_name:
            supplier = db.execute(
                select(Supplier).where(Supplier.name == vendor_name)
            ).scalar_one_or_none()
            if not supplier:
                supplier = Supplier(name=vendor_name)
                db.add(supplier)
                db.flush()
    else:
        customer_name = metadata.get("customer_name")
        if customer_name:
            customer = db.execute(
                select(Customer).where(Customer.name == customer_name)
            ).scalar_one_or_none()
            if not customer:
                customer = Customer(name=customer_name)
                db.add(customer)
                db.flush()

    eta_value = date.today()
    eta_str = metadata.get("eta")
    if eta_str:
        try:
            eta_value = datetime.fromisoformat(eta_str).date()
        except ValueError:
            pass

    created_at_value = date.today()
    created_at_str = metadata.get("created_at")
    if created_at_str:
        try:
            created_at_value = datetime.fromisoformat(created_at_str).date()
        except ValueError:
            pass

    if is_purchase_order:
        final_order_type = OrderType.INCOMING.value
    else:
        outgoing_type_options = {
            OrderType.INSTALLATION.value,
            OrderType.WILL_CALL.value,
            OrderType.DELIVERY.value,
            OrderType.SHIPMENT.value,
        }
        if order_type and order_type in outgoing_type_options:
            final_order_type = order_type
        else:
            final_order_type = OrderType.INSTALLATION.value

    default_description = metadata.get(
        "memo", f"QB {entity_type.replace('_', ' ').title()} #{reference_number}"
    )
    order = Order(
        type=final_order_type,
        customer_id=customer.id if customer else None,
        supplier_id=supplier.id if supplier else None,
        warehouse_id=warehouse_id,
        external_order_number=reference_number,
        qb_doc_type=normalized_doc_type,
        description=description_override or default_description,
        status=OrderStatus.PENDING.value,
        eta=eta_value,
        created_at=created_at_value,
    )
    db.add(order)
    db.flush()
    order.order_number = f"AFC-{order.id:06d}"

    created_items: list[dict[str, Any]] = []
    new_products: list[dict[str, Any]] = []
    skipped_items: list[dict[str, Any]] = []
    position = 0

    for qb_line in line_items:
        if qb_line.get("is_separator"):
            description = qb_line.get("description", "")
            separator_type = OrderItemType.UNIT_SEPARATOR.value
            if description:
                desc_lower = description.lower()
                replaced, count = re.subn(r"(?:&#149;?|\x95)", "•", description)
                if count:
                    separator_type = OrderItemType.SECTION_SEPARATOR.value
                    description = replaced
                elif "building" in desc_lower or "bldg" in desc_lower or "•" in description:
                    separator_type = OrderItemType.SECTION_SEPARATOR.value

            separator = OrderItem(
                order_id=order.id,
                product_id=None,
                type=separator_type,
                quantity_ordered=0,
                quantity_fulfilled=0,
                note=description,
                position=position,
            )
            db.add(separator)
            db.flush()

            created_items.append({
                "id": separator.id,
                "order_id": order.id,
                "product_id": None,
                "type": separator_type,
                "part_number": "",
                "quantity_ordered": 0,
                "quantity_fulfilled": 0,
                "quantity_pending": 0,
                "status": "pending",
                "note": description,
                "position": position,
                "on_hand": None,
                "reserved": None,
                "available": None,
                "is_media": False,
            })
            position += 1
            continue

        item_name = qb_line.get("name", "").strip()
        if not item_name:
            skipped_items.append({"name": "(empty)", "reason": "Item name is empty or missing"})
            position += 1
            continue

        blocked = db.execute(
            select(BlockedItem).where(func.lower(BlockedItem.name) == item_name.lower())
        ).scalar_one_or_none()
        if blocked:
            skipped_items.append({"name": item_name, "reason": "Item is blocked"})
            position += 1
            continue

        product = find_product_by_name(db, item_name)
        if not product:
            qb_supplier = db.execute(
                select(Supplier).where(Supplier.name == Config.QB_SUPPLIER_NAME)
            ).scalar_one_or_none()
            if not qb_supplier:
                qb_supplier = Supplier(name=Config.QB_SUPPLIER_NAME)
                db.add(qb_supplier)
                db.flush()

            qb_category = db.execute(
                select(StockItemCategory).where(StockItemCategory.name == Config.QB_SUPPLIER_NAME)
            ).scalar_one_or_none()
            if not qb_category:
                qb_category = StockItemCategory(name=Config.QB_SUPPLIER_NAME)
                db.add(qb_category)
                db.flush()

            new_stock_item = StockItem(
                name=item_name,
                supplier_id=qb_supplier.id,
                category_id=qb_category.id,
            )
            db.add(new_stock_item)
            db.flush()

            product = Product(category_id=3, reference_id=new_stock_item.id)
            db.add(product)
            db.flush()

            warehouses = db.execute(select(Warehouse)).scalars().all()
            for wh in warehouses:
                db.add(
                    Quantity(
                        product_id=product.id,
                        warehouse_id=wh.id,
                        on_hand=0,
                        reserved=0,
                        ordered=0,
                        location=0,
                    )
                )
            db.flush()

            new_products.append({
                "name": item_name,
                "stock_item_id": new_stock_item.id,
                "product_id": product.id,
            })

        qty_ordered = qb_line.get("quantity", 0)
        if qty_ordered < 0:
            qty_ordered = 0

        item_type = OrderItemType.PRODUCT_ITEM.value
        no_stock_deduction = False if is_purchase_order else bool(product.default_no_stock_deduction)
        if not is_purchase_order and getattr(product, "media", None) is not None:
            media_default_desc = (product.media.description or "").strip()
            qb_desc = (qb_line.get("description") or "").strip()
            if qb_desc.lower() != media_default_desc.lower():
                no_stock_deduction = True

        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            type=item_type,
            quantity_ordered=int(qty_ordered),
            quantity_fulfilled=0,
            note=qb_line.get("description"),
            position=position,
            no_stock_deduction=no_stock_deduction,
        )
        db.add(order_item)
        db.flush()

        qty_record = getattr(product, "quantity", None)
        created_items.append({
            "id": order_item.id,
            "order_id": order.id,
            "product_id": product.id,
            "type": item_type,
            "part_number": item_name,
            "quantity_ordered": int(qty_ordered),
            "quantity_fulfilled": 0,
            "quantity_pending": 0,
            "status": "pending",
            "note": qb_line.get("description"),
            "position": position,
            "on_hand": qty_record.on_hand if qty_record else None,
            "reserved": qty_record.reserved if qty_record else None,
            "available": qty_record.available if qty_record else None,
            "is_media": getattr(product, "media", None) is not None,
            "no_stock_deduction": order_item.no_stock_deduction,
        })
        position += 1

    if not is_purchase_order:
        tracker = OrderTracker(
            order_id=order.id,
            warehouse_id=warehouse_id,
            current_department=Department.SALES.value,
            step_index=0,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(tracker)

    safe_commit(db, "creating order from QuickBooks")

    return CreateOrderFromQBResult(
        order=order,
        customer=customer,
        supplier=supplier,
        created_items=created_items,
        new_products=new_products,
        skipped_items=skipped_items,
        metadata=metadata,
    )
