from flask import Blueprint, g, jsonify, request
from sqlalchemy import select, func, null
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError, DatabaseError
from app.api.Schemas.order_schema import OrderSchema
from database.models import Customer, Supplier, OrderType, OrderStatus, OrderItemType, Transaction, TransactionState, OUTGOING_TYPES, VALID_ORDER_TYPES
from database.models import Order, OrderItem, Product, AirFilter, StockItem, StockItemCategory, Quantity, OrderTracker, Department, BlockedItem
from marshmallow import ValidationError
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
import re
import requests

from app.config import Config
from app.api.validation import (
    validate_positive_integer,
    validate_string,
    validate_enum,
    validate_pagination,
    sanitize_search_string,
    ValidationError as CustomValidationError
)
from app.api.error_handling import (
    handle_database_error,
    handle_validation_error,
    handle_external_service_error,
    safe_commit,
    ResourceNotFoundError,
    DuplicateResourceError,
    ExternalServiceError
)
from app.api.qb_xml_parser import parse_qb_line_items, extract_qb_metadata

order_bp = Blueprint("orders", __name__)
order_schema = OrderSchema()
order_list_schema = OrderSchema(many=True)

# GET all orders (paginated, filterable)
@order_bp.route("/orders", methods=["GET"])
def get_orders() -> Tuple[Any, int]:
    """
    Retrieve all orders with pagination and optional filters.
    
    Query Parameters:
        page (int): Page number (default: 1)
        limit (int): Items per page (default: 25, max: 100)
        type (str): Filter by order type ("incoming" | "outgoing")
        status (str): Filter by order status
        search (str): Search keyword in order_number
    
    Returns:
        JSON response with paginated orders and metadata
    """
    db = g.db
    
    try:
        # Validate pagination parameters
        page, limit = validate_pagination(
            request.args.get("page"),
            request.args.get("limit"),
            max_limit=Config.MAX_PAGE_SIZE,
            default_page=1,
            default_limit=Config.DEFAULT_PAGE_SIZE
        )
        offset = (page - 1) * limit
        
        # Sanitize and validate optional filters
        type_filter = request.args.get("type")
        status_filter = request.args.get("status")
        search = sanitize_search_string(request.args.get("search", ""))
        
        # Build query
        query = select(Order)
        
        if type_filter:
            # Validate enum value if needed
            query = query.where(Order.type == type_filter)
        
        if status_filter:
            query = query.where(Order.status == status_filter)
        
        if search:
            query = query.where(Order.order_number.ilike(f"%{search}%"))

        # Scope to the active warehouse
        query = query.where(Order.warehouse_id == g.active_warehouse_id)
        
        query = query.order_by(Order.created_at.desc()).offset(offset).limit(limit)
        results = db.execute(query).scalars().all()
        
        total = db.execute(
            select(func.count()).select_from(Order).where(Order.warehouse_id == g.active_warehouse_id)
        ).scalar()
        
        return jsonify({
            "page": page,
            "limit": limit,
            "total": total,
            "results": order_list_schema.dump(results)
        }), 200
        
    except CustomValidationError as e:
        return handle_validation_error(e)
    except DatabaseError as e:
        return handle_database_error(e, "fetching orders")
    except Exception as e:
        return jsonify({"error": "Unexpected error", "details": str(e)}), 500


# GET single order with items
@order_bp.route("/orders/<int:order_id>", methods=["GET"])
def get_order(order_id: int) -> Tuple[Any, int]:
    """
    Retrieve a single order by ID with all its items.
    
    Args:
        order_id: The ID of the order to retrieve
    
    Returns:
        JSON response with order details and items
    """
    db = g.db
    
    try:
        # Validate order_id
        order_id = validate_positive_integer(order_id, "order_id")
        
        order = db.get(Order, order_id)
        if not order:
            raise ResourceNotFoundError("Order", order_id)

        # Determine customer or supplier name
        cs_name = None
        cs_id = None
        if order.type in OUTGOING_TYPES and order.customer:
            cs_id = order.customer.id
            cs_name = order.customer.name
        elif order.type == OrderType.INCOMING.value and order.supplier:
            cs_id = order.supplier.id
            cs_name = order.supplier.name
        
        return jsonify({
            "id": order.id,
            "order_number": order.order_number,
            "external_order_number": order.external_order_number,
            "type": order.type,
            "cs_id": cs_id,
            "cs_name": cs_name,
            "status": order.status,
            "description": order.description,
            "created_at": order.created_at.strftime(Config.DATE_FORMAT),
            "completed_at": (
                order.completed_at.strftime(Config.DATE_FORMAT)
                if order.completed_at else None
            ),
            "eta": (
                order.eta.strftime(Config.DATE_FORMAT)
                if order.eta else None
            ),
            "is_paid": order.is_paid,
            "is_invoiced": order.is_invoiced,
        }), 200
        
    except ResourceNotFoundError as e:
        return jsonify(e.to_dict()), e.status_code
    except CustomValidationError as e:
        return handle_validation_error(e)
    except Exception as e:
        return jsonify({"error": "Unexpected error", "details": str(e)}), 500


# GET order items
@order_bp.route("/orders/<int:order_id>/items", methods=["GET"])
def get_order_items(order_id):
    db = g.db
    order = db.get(Order, order_id)

    if not order:
        return jsonify({"error": "Order not found"}), 404

    # Sort items by position
    sorted_items = sorted(order.items, key=lambda x: x.position)

    # Batch fetch pending transaction quantities per order item (avoid N+1 queries)
    pending_by_item: dict = {}
    if sorted_items:
        item_ids = [item.id for item in sorted_items]
        pending_rows = db.execute(
            select(
                Transaction.order_item_id,
                func.sum(func.abs(Transaction.quantity_delta)).label("pending_qty")
            )
            .where(Transaction.order_item_id.in_(item_ids))
            .where(Transaction.state == "pending")
            .group_by(Transaction.order_item_id)
        ).all()
        pending_by_item = {row.order_item_id: row.pending_qty for row in pending_rows}

    items = []
    for item in sorted_items:
        if item.type in ("Unit_Separator", "Section_Separator"):
            # Separator items don't have a product
            part_number = ""
            on_hand = None
            reserved = None
            available = None
            quantity_pending = 0
            is_media = False
        else:
            product = item.product

            if product and product.category.name == "Air Filters":
                part_number = product.air_filter.part_number
            elif product and product.media is not None:
                part_number = product.media.part_number
            elif product:
                part_number = f"Product #{product.id}"
            else:
                part_number = "Unknown product"

            qty_record = product.quantity if product else None
            on_hand = qty_record.on_hand if qty_record else None
            reserved = qty_record.reserved if qty_record else None
            available = qty_record.available if qty_record else None
            quantity_pending = pending_by_item.get(item.id, 0)
            is_media = product is not None and product.media is not None

        items.append({
            "id": item.id,
            "order_id": item.order_id,
            "product_id": item.product_id,
            "type": item.type,
            "part_number": part_number,
            "quantity_ordered": item.quantity_ordered,
            "quantity_fulfilled": item.quantity_fulfilled,
            "quantity_pending": quantity_pending,
            "status": item.status,
            "note": item.note,
            "position": item.position,
            "on_hand": on_hand,
            "reserved": reserved,
            "available": available,
            "is_media": is_media,
        })

    return jsonify(items), 200


# GET serialized order items string
@order_bp.route("/orders/<int:order_id>/serialize", methods=["GET"])
def serialize_order(order_id):
    db = g.db
    order = db.get(Order, order_id)

    if not order:
        return jsonify({"error": "Order not found"}), 404

    sorted_items = sorted(order.items, key=lambda x: x.position)

    # Optional: filter to specific item IDs (comma-separated query param)
    item_ids_param = request.args.get("item_ids")
    if item_ids_param:
        try:
            item_ids = set(int(x) for x in item_ids_param.split(","))
        except ValueError:
            return jsonify({"error": "Invalid item_ids parameter: must be comma-separated integers"}), 400
        sorted_items = [i for i in sorted_items if i.id in item_ids]

    blank_row = "||||||||||||"
    lines = []

    for item in sorted_items:
        if item.type == OrderItemType.SECTION_SEPARATOR.value:
            description = item.note or ""
            lines.append(blank_row)
            lines.append(f"||||{description}||")
        elif item.type == OrderItemType.UNIT_SEPARATOR.value:
            description = item.note or ""
            lines.append(f"||||{description}||")
        else:
            # Product_Item or Sales_Item
            product = item.product
            if product and product.category.name == "Air Filters":
                part_number = product.air_filter.part_number
            elif product:
                part_number = f"Product #{product.id}"
            else:
                part_number = "Unknown product"

            qty = item.quantity_ordered
            lines.append(f"{qty}||{part_number}||||||||||")

    serialized = "".join(lines)
    return jsonify({"serialized": serialized}), 200


# Create new order
@order_bp.route("/orders", methods=["POST"])
def create_order():
    db = g.db

    try:
        data = order_schema.load(request.get_json())
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    order = Order.from_dict(data)

    # Assign active warehouse to the order
    order.warehouse_id = g.active_warehouse_id

    # ===============================
    # Generate AFC order number
    # ===============================
    db.add(order)
    db.flush()  # ensures order.id is available

    order.order_number = f"AFC-{order.id:06d}"

    # ===============================
    # Validate customer / supplier
    # ===============================
    if order.type in OUTGOING_TYPES:
        if not order.customer_id:
            return jsonify({
                "error": "customer_id is required for outgoing orders"
            }), 400
        order.supplier_id = None
        # Auto-create tracker for outgoing orders starting at SALES
        tracker = OrderTracker(
            order_id=order.id,
            current_department=Department.SALES.value,
            step_index=0,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(tracker)

    elif order.type == OrderType.INCOMING.value:
        if not order.supplier_id:
            return jsonify({
                "error": "supplier_id is required for incoming orders"
            }), 400
        order.customer_id = None

    db.commit()

    return jsonify(order_schema.dump(order)), 201



# PATCH: Force update status (recalculate)
@order_bp.route("/orders/<int:order_id>/status", methods=["PATCH"])
def update_order_status(order_id):
    db = g.db
    order = db.get(Order, order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    order.update_status()
    db.commit()

    return jsonify({
        "message": "Order status updated.",
        "status": order.status
    }), 200

@order_bp.route("/orders/<int:order_id>", methods=["DELETE"])
def delete_order(order_id: int):
    """
    Delete an order only if it has no transactions on any of its items.
    """
    db = g.db

    try:
        order_id = validate_positive_integer(order_id, "order_id")
        order = db.get(Order, order_id)

        if not order:
            raise ResourceNotFoundError("Order", order_id)

        # Prevent deletion if any item has transactions
        has_transactions = any(len(item.transactions) > 0 for item in order.items)
        if has_transactions:
            return jsonify({
                "error": "Cannot delete order with existing transactions"
            }), 409

        db.delete(order)
        db.commit()

        return jsonify({"message": "Order deleted"}), 200

    except ResourceNotFoundError as e:
        return jsonify(e.to_dict()), e.status_code
    except CustomValidationError as e:
        return handle_validation_error(e)
    except Exception as e:
        return jsonify({"error": "Unexpected error", "details": str(e)}), 500


@order_bp.route("/orders/<int:order_id>", methods=["PATCH"])
def patch_order(order_id):
    db = g.db
    order = db.get(Order, order_id)

    if not order:
        return jsonify({"error": "Order not found"}), 404

    data = request.get_json() or {}

    # ===============================
    # ❌ Disallowed fields
    # ===============================
    for forbidden in ("status", "completed_at", "order_number"):
        if forbidden in data:
            return jsonify({
                "error": f"'{forbidden}' cannot be modified"
            }), 400
        
    # ===============================
    # Type validation
    # ===============================

    if "type" in data:
        new_type = data["type"]

        if new_type not in VALID_ORDER_TYPES:
            return jsonify({"error": "Invalid order type"}), 400

        order.type = new_type

    # ===============================
    # Customer / Supplier assignment
    # ===============================
    if "cs_id" in data:
        cs_id = data["cs_id"]

        if not cs_id:
            return jsonify({"error": "cs_id cannot be empty"}), 400

        if order.type in OUTGOING_TYPES:
            order.customer_id = cs_id
            order.supplier_id = None
        elif order.type == OrderType.INCOMING.value:
            order.supplier_id = cs_id
            order.customer_id = None

    # ===============================
    # Description
    # ===============================
    if "description" in data:
        order.description = data["description"]

    # ===============================
    # Created At (date only)
    # ===============================
    if "created_at" in data:
        try:
            order.created_at = datetime.strptime(
                data["created_at"], "%Y-%m-%d"
            )
        except ValueError:
            return jsonify({
                "error": "created_at must be YYYY-MM-DD"
            }), 400

    # ===============================
    # ETA (optional, must be >= created_at)
    # ===============================
    if "eta" in data:
        if data["eta"] is None:
            order.eta = None
        else:
            try:
                eta = datetime.strptime(
                    data["eta"], "%Y-%m-%d"
                ).date()
                created = order.created_at.date()

                if eta < created:
                    return jsonify({
                        "error": "ETA cannot be earlier than created date"
                    }), 400

                order.eta = eta
            except ValueError:
                return jsonify({
                    "error": "eta must be YYYY-MM-DD"
                }), 400
            
    if "supplier_id" in data:
        order.supplier_id = data["supplier_id"]
    
    if "external_order_number" in data:
        order.external_order_number = data["external_order_number"]

    if "is_paid" in data:
        order.is_paid = bool(data["is_paid"])

    if "is_invoiced" in data:
        order.is_invoiced = bool(data["is_invoiced"])

    db.commit()

    # ===============================
    # Return updated order (same shape as GET)
    # ===============================
    cs_name = None
    if order.type in OUTGOING_TYPES and order.customer:
        cs_name = order.customer.name
    elif order.type == OrderType.INCOMING.value and order.supplier:
        cs_name = order.supplier.name

    return jsonify({
        "id": order.id,
        "order_number": order.order_number,
        "type": order.type,
        "cs_name": cs_name,
        "status": order.status,
        "description": order.description,
        "created_at": order.created_at.strftime("%Y-%m-%d"),
        "completed_at": (
            order.completed_at.strftime("%Y-%m-%d")
            if order.completed_at else None
        ),
        "eta": (
            order.eta.strftime("%Y-%m-%d")
            if order.eta else None
        ),
    }), 200

def parse_date(date_str: str):
    return datetime.strptime(date_str, "%Y-%m-%d")

# ===============================
# SEARCH
# ===============================

@order_bp.route("/orders/search", methods=["GET"])
def search_orders():
    db = g.db

    search = request.args.get("search")
    order_number = request.args.get("order_number")
    external_order_number = request.args.get("external_order_number")
    description = request.args.get("description")
    customer_name = request.args.get("customer_name")
    supplier_name = request.args.get("supplier_name")
    order_type = request.args.get("type")
    status = request.args.get("status")
    
    # Date filters for created_at
    created_from = request.args.get("created_from")
    created_to = request.args.get("created_to")
    
    # Date filters for completed_at
    completed_from = request.args.get("completed_from")
    completed_to = request.args.get("completed_to")
    
    # Product filtering - comma separated product IDs
    product_ids = request.args.get("product_ids")

    # Parse product_id_list early so it can be used in both the SELECT subquery and the WHERE filter
    product_id_list = []
    if product_ids:
        try:
            product_id_list = [int(pid.strip()) for pid in product_ids.split(",") if pid.strip()]
        except (ValueError, AttributeError):
            pass

    page = request.args.get("page", default=1, type=int)
    limit = request.args.get("limit", default=25, type=int)
    offset = (page - 1) * limit

    # Build quantity subquery: when product_ids are provided, sum the ordered quantity
    # for those products within each order so the frontend can use it for stock projection
    if product_id_list:
        qty_subquery = (
            select(func.sum(OrderItem.quantity_ordered))
            .where(
                OrderItem.order_id == Order.id,
                or_(
                    OrderItem.product_id.in_(product_id_list),
                    OrderItem.child_product_id.in_(product_id_list),
                ),
            )
            .correlate(Order)
            .scalar_subquery()
            .label("quantity")
        )
    else:
        qty_subquery = null().label("quantity")

    query = (
        select(
            Order.id,
            Order.order_number,
            Order.external_order_number,
            Order.type,
            Order.status,
            Order.description,
            Order.created_at,
            Order.completed_at,
            Order.eta,
            Customer.name.label("customer_name"),
            Supplier.name.label("supplier_name"),
            qty_subquery,
        )
        .outerjoin(Customer, Order.customer_id == Customer.id)
        .outerjoin(Supplier, Order.supplier_id == Supplier.id)
    )

    filters = []

    if order_type:
        # "outgoing" is a legacy/convenience filter matching all outgoing-equivalent types
        if order_type == "outgoing":
            filters.append(Order.type.in_(OUTGOING_TYPES))
        else:
            filters.append(Order.type == order_type)

    if status:
        filters.append(Order.status == status)

    if search:
        filters.append(
            or_(
                Order.order_number.ilike(f"%{search}%"),
                Order.external_order_number.ilike(f"%{search}%"),
                Customer.name.ilike(f"%{search}%"),
                Supplier.name.ilike(f"%{search}%"),
            )
        )

    if order_number:
        # User provides only the significant digits (e.g. "123")
        # Pad to 6 digits and prepend AFC- to match format "AFC-000123"
        digits = order_number.strip()
        padded = digits.lstrip("0") or "0"
        padded = padded.zfill(6)
        filters.append(Order.order_number.ilike(f"%AFC-{padded}%"))

    if external_order_number:
        filters.append(Order.external_order_number.ilike(f"%{external_order_number}%"))

    if description:
        filters.append(Order.description.ilike(f"%{description}%"))

    # Separate customer and supplier name filters
    if customer_name:
        filters.append(Customer.name.ilike(f"%{customer_name}%"))
    
    if supplier_name:
        filters.append(Supplier.name.ilike(f"%{supplier_name}%"))
    
    # Date filters for created_at
    if created_from:
        try:
            from_date = parse_date(created_from)
            filters.append(Order.created_at >= from_date)
        except ValueError:
            pass
    
    if created_to:
        try:
            to_date = parse_date(created_to)
            # Add one day to include the entire end date
            to_date = to_date + timedelta(days=1)
            filters.append(Order.created_at < to_date)
        except ValueError:
            pass
    
    # Date filters for completed_at
    if completed_from:
        try:
            from_date = parse_date(completed_from)
            filters.append(Order.completed_at >= from_date)
        except ValueError:
            pass
    
    if completed_to:
        try:
            to_date = parse_date(completed_to)
            # Add one day to include the entire end date
            to_date = to_date + timedelta(days=1)
            filters.append(Order.completed_at < to_date)
        except ValueError:
            pass
    
    # Product filtering - filter orders containing specific products or child products
    if product_id_list:
        # Use a subquery to find orders that contain any of the specified products
        # Check both product_id and child_product_id
        product_filter_subquery = (
            select(OrderItem.order_id)
            .where(
                or_(
                    OrderItem.product_id.in_(product_id_list),
                    OrderItem.child_product_id.in_(product_id_list)
                )
            )
            .distinct()
        )
        filters.append(Order.id.in_(product_filter_subquery))

    if filters:
        query = query.where(and_(*filters))

    # ---------------- Count ----------------
    total = db.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar()

    # ---------------- Page ----------------
    rows = db.execute(
        query.order_by(Order.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).mappings().all()

    output = []
    for row in rows:
        row = dict(row)
        row["cs_name"] = (
            row.pop("customer_name")
            or row.pop("supplier_name")
        )
        if row.get("eta") is not None:
            row["eta"] = row["eta"].strftime("%Y-%m-%d")
        output.append(row)

    return jsonify({
        "page": page,
        "limit": limit,
        "count": len(output),
        "total": total,
        "results": output,
    }), 200

# ===============================
# ALLOCATE ALL
# ===============================

@order_bp.route("/orders/<int:order_id>/allocate-all", methods=["POST"])
def allocate_all(order_id):
    db = g.db

    order = db.get(Order, order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    if order.status == OrderStatus.COMPLETED.value:
        return jsonify({"error": "Cannot allocate a completed order"}), 400

    created = []

    for item in order.items:

        # Sum existing pending allocations
        pending_qty = sum(
            abs(tx.quantity_delta)
            for tx in item.transactions
            if tx.state == TransactionState.PENDING.value
        )

        remaining = (
            item.quantity_ordered
            - item.quantity_fulfilled
            - pending_qty
        )

        if remaining <= 0:
            continue

        qty_delta = (
            -remaining
            if order.type in OUTGOING_TYPES
            else remaining
        )

        txn = Transaction(
            product_id=item.product_id,
            order_id=order.id,
            order_item_id=item.id,
            warehouse_id=order.warehouse_id,
            quantity_delta=qty_delta,
            reason="allocation",
            state=TransactionState.PENDING.value,
        )

        # Use the order's warehouse quantity for the pending effect
        qty = db.execute(
            select(Quantity).where(
                (Quantity.product_id == item.product_id) &
                (Quantity.warehouse_id == order.warehouse_id)
            )
        ).scalar_one_or_none() if item.product_id else None

        # Apply pending effect
        if qty is not None:
            if qty_delta < 0:
                qty.reserved += remaining
            else:
                qty.ordered += remaining

        db.add(txn)
        created.append(txn)

    db.commit()

    return jsonify({
        "message": f"{len(created)} items allocated",
        "transactions_created": len(created),
    }), 201


# ===============================
# CREATE ORDER FROM QUICKBOOKS
# ===============================

def get_or_create_qb_supplier(db):
    """
    Get or create a default supplier for QuickBooks items.
    
    Args:
        db: Database session
        
    Returns:
        Supplier object
        
    Raises:
        IntegrityError: If supplier creation fails due to database constraint
        DatabaseError: If database operation fails
    """
    supplier = db.execute(
        select(Supplier).where(Supplier.name == Config.QB_SUPPLIER_NAME)
    ).scalar_one_or_none()
    
    if not supplier:
        try:
            supplier = Supplier(name=Config.QB_SUPPLIER_NAME)
            db.add(supplier)
            db.flush()
        except IntegrityError:
            # Re-query in case another transaction created it concurrently
            supplier = db.execute(
                select(Supplier).where(Supplier.name == Config.QB_SUPPLIER_NAME)
            ).scalar_one_or_none()
            if not supplier:
                raise
        except DatabaseError:
            # Re-query in case of other database errors
            supplier = db.execute(
                select(Supplier).where(Supplier.name == Config.QB_SUPPLIER_NAME)
            ).scalar_one_or_none()
            if not supplier:
                raise
    
    return supplier


@order_bp.route("/orders/from-qb", methods=["POST"])
def create_order_from_qb():
    """
    Create a new order from QuickBooks data.
    
    Items in the "blocked_items" table are skipped. Unmatched items (not found in
    air_filters or stock_items) are automatically added to the stock_items table
    and associated with the configured QuickBooks supplier.
    
    Expects JSON body with:
    {
        "reference_number": "8800",
        "qb_doc_type": "sales_order" | "estimate" | "invoice" | "purchase_order",
        "order_type": "installation" | "will_call" | "delivery" | "shipment"  (optional, for non-purchase orders)
    }
    
    Returns:
        JSON with order details, including:
        - new_products: Array of newly created StockItem products
        - new_products_created: Count of new products created
        - created_items: All order items created
        - skipped_items: Items that were skipped (if any)
    """
    db = g.db
    data = request.get_json() or {}
    
    # Validate required fields
    reference_number = data.get("reference_number")
    qb_doc_type = data.get("qb_doc_type", "").lower()
    order_type_override = data.get("order_type")
    
    if not reference_number:
        return jsonify({"error": "reference_number is required"}), 400
    
    # Validate reference_number format (alphanumeric, hyphens, underscores)
    if not isinstance(reference_number, str) or not reference_number.strip():
        return jsonify({"error": "reference_number must be a non-empty string"}), 400
    
    reference_number = reference_number.strip()
    
    # Check for duplicate order
    existing_order = db.execute(
        select(Order).where(Order.external_order_number == reference_number)
    ).scalar_one_or_none()
    
    if existing_order:
        raise DuplicateResourceError("Order", "external_order_number", reference_number)
    
    # Validate qb_doc_type
    valid_types = ["sales_order", "salesorder", "estimate", "invoice", "purchase_order", "purchaseorder"]
    if qb_doc_type not in valid_types:
        raise CustomValidationError(
            f"qb_doc_type must be one of: {', '.join(valid_types)}"
        )
    
    # Normalize qb_doc_type (salesorder -> sales_order, purchaseorder -> purchase_order)
    entity_type = qb_doc_type.replace("salesorder", "sales_order").replace("purchaseorder", "purchase_order")
    
    # Query QuickBooks via the QB agent
    try:
        headers = {}
        if Config.QB_API_KEY:
            headers["X-API-Key"] = Config.QB_API_KEY
        
        response = requests.post(
            f"{Config.QB_AGENT_URL}/jobs",
            json={
                "op": "query",
                "entity": entity_type,
                "params": {"refnumber": reference_number}
            },
            headers=headers,
            timeout=Config.QB_REQUEST_TIMEOUT
        )
        response.raise_for_status()
        qb_result = response.json()
    except requests.exceptions.Timeout:
        raise ExternalServiceError(
            "QuickBooks Agent",
            f"Request timed out after {Config.QB_REQUEST_TIMEOUT} seconds"
        )
    except requests.exceptions.ConnectionError:
        raise ExternalServiceError(
            "QuickBooks Agent",
            "Connection refused. Is the QB Agent running?"
        )
    except requests.RequestException as e:
        raise ExternalServiceError("QuickBooks Agent", str(e))
    
    # Check if QB query was successful
    if not qb_result.get("success"):
        return jsonify({
            "error": "QuickBooks query failed",
            "qb_error": qb_result.get("errorMessage"),
            "qb_error_code": qb_result.get("errorCode")
        }), 400
    
    # Parse the QBXML response
    qbxml_response = qb_result.get("qbxmlResponse", "")
    
    try:
        line_items = parse_qb_line_items(qbxml_response, entity_type)
        metadata = extract_qb_metadata(qbxml_response, entity_type)
    except ValueError as e:
        return jsonify({
            "error": "Failed to parse QuickBooks response",
            "details": str(e)
        }), 400
    
    if not line_items:
        return jsonify({
            "error": "No line items found in QuickBooks response"
        }), 400
    
    # Determine order type based on QB document type
    is_purchase_order = entity_type in ("purchase_order", "purchaseorder")
    
    # Find or create supplier/customer based on QB vendor/customer name
    customer = None
    supplier = None
    
    if is_purchase_order:
        # Purchase orders are INCOMING: find or create supplier from vendor name
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
        # Sales orders/estimates/invoices are OUTGOING: find or create customer
        customer_name = metadata.get("customer_name")
        if customer_name:
            customer = db.execute(
                select(Customer).where(Customer.name == customer_name)
            ).scalar_one_or_none()
            
            # Create customer if doesn't exist
            if not customer:
                customer = Customer(name=customer_name)
                db.add(customer)
                db.flush()
    
    # Parse ETA from metadata (set by QB ExpectedDate for purchase orders)
    eta_value = None
    eta_str = metadata.get("eta")
    if eta_str:
        try:
            eta_value = datetime.strptime(eta_str, "%Y-%m-%d").date()
        except ValueError:
            pass
    
    # Determine the final order type
    if is_purchase_order:
        final_order_type = OrderType.INCOMING.value
    else:
        # Validate the order_type_override if provided
        outgoing_type_options = {
            OrderType.INSTALLATION.value,
            OrderType.WILL_CALL.value,
            OrderType.DELIVERY.value,
            OrderType.SHIPMENT.value,
        }
        if order_type_override and order_type_override in outgoing_type_options:
            final_order_type = order_type_override
        else:
            final_order_type = OrderType.INSTALLATION.value  # default for non-PO QB orders

    # Create the order
    order = Order(
        type=final_order_type,
        customer_id=customer.id if customer else None,
        supplier_id=supplier.id if supplier else None,
        external_order_number=reference_number,
        description=metadata.get("memo", f"QB {entity_type.replace('_', ' ').title()} #{reference_number}"),
        status=OrderStatus.PENDING.value,
        eta=eta_value
    )
    
    db.add(order)
    db.flush()  # Get order.id
    
    # Generate AFC order number
    order.order_number = f"AFC-{order.id:06d}"
    
    # Process line items
    created_items = []
    new_products = []
    skipped_items = []
    position = 0
    
    try:
        for qb_line in line_items:
            if qb_line.get("is_separator"):
                # Determine separator type based on description
                description = qb_line.get("description", "")
                separator_type = OrderItemType.UNIT_SEPARATOR.value
                if description:
                    desc_lower = description.lower()
                    replaced, count = re.subn(r'(?:&#149;?|\x95)', '•', description)
                    if count:
                        separator_type = OrderItemType.SECTION_SEPARATOR.value
                        description = replaced
                    elif "building" in desc_lower or "bldg" in desc_lower or "•" in description:
                        separator_type = OrderItemType.SECTION_SEPARATOR.value

                # Create separator item
                separator = OrderItem(
                    order_id=order.id,
                    product_id=None,
                    type=separator_type,
                    quantity_ordered=0,
                    quantity_fulfilled=0,
                    note=description,
                    position=position
                )
                db.add(separator)
                created_items.append({
                    "type": separator_type,
                    "description": description
                })
                position += 1
            else:
                # Find product by name (from QB item name)
                item_name = qb_line.get("name", "").strip()
                
                # Validate item name
                if not item_name:
                    skipped_items.append({
                        "name": "(empty)",
                        "reason": "Item name is empty or missing"
                    })
                    position += 1
                    continue
                
                # Check if item is blocked
                blocked = db.execute(
                    select(BlockedItem).where(
                        func.lower(BlockedItem.name) == item_name.lower()
                    )
                ).scalar_one_or_none()

                if blocked:
                    skipped_items.append({
                        "name": item_name,
                        "reason": "Item is blocked"
                    })
                    position += 1
                    continue

                product = find_product_by_name(db, item_name)
                
                if not product:
                    # Item not found in air_filters or stock_items — auto-add to stock_items
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
                        category_id=qb_category.id
                    )
                    db.add(new_stock_item)
                    db.flush()

                    product = Product(
                        category_id=3,  # Product category ID for stock items (matches stock_items.py convention)
                        reference_id=new_stock_item.id
                    )
                    db.add(product)
                    db.flush()

                    quantity = Quantity(product_id=product.id, on_hand=0, reserved=0, ordered=0, location=0)
                    db.add(quantity)
                    db.flush()

                    new_products.append({
                        "name": item_name,
                        "stock_item_id": new_stock_item.id,
                        "product_id": product.id
                    })

                
                # Validate quantity
                quantity = qb_line.get("quantity", 0)
                if quantity < 0:
                    quantity = 0
                
                # Determine order item type: detect media cut by comparing descriptions
                item_type = OrderItemType.PRODUCT_ITEM.value
                if product.media is not None:
                    media_default_desc = (product.media.description or "").strip()
                    qb_desc = (qb_line.get("description") or "").strip()
                    if qb_desc.lower() != media_default_desc.lower():
                        item_type = OrderItemType.MEDIA_CUT.value

                # Create order item
                order_item = OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    type=item_type,
                    quantity_ordered=int(quantity),
                    quantity_fulfilled=0,
                    note=qb_line.get("description"),
                    position=position
                )
                db.add(order_item)
                created_items.append({
                    "type": "product",
                    "name": item_name,
                    "quantity": quantity
                })
                position += 1

        # Auto-create tracker for outgoing orders starting at SALES
        if not is_purchase_order:
            tracker = OrderTracker(
                order_id=order.id,
                current_department=Department.SALES.value,
                step_index=0,
                updated_at=datetime.now(timezone.utc),
            )
            db.add(tracker)

        safe_commit(db, "creating order from QuickBooks")
        
    except (CustomValidationError, DuplicateResourceError, ExternalServiceError) as e:
        db.rollback()
        return jsonify(e.to_dict()), e.status_code
    except IntegrityError as e:
        db.rollback()
        return handle_database_error(e, "creating order from QuickBooks")
    except DatabaseError as e:
        db.rollback()
        return handle_database_error(e, "creating order from QuickBooks")
    except requests.RequestException as e:
        db.rollback()
        return handle_external_service_error(e, "QuickBooks Agent")
    except Exception as e:
        db.rollback()
        # Log the full error for debugging but return generic message
        return jsonify({
            "error": "Failed to create order from QuickBooks",
            "details": str(e)
        }), 500
    
    return jsonify({
        "message": "Order created successfully from QuickBooks",
        "order_id": order.id,
        "order_number": order.order_number,
        "external_order_number": order.external_order_number,
        "customer_name": customer.name if customer else None,
        "vendor_name": supplier.name if supplier else None,
        "eta": order.eta.strftime("%Y-%m-%d") if order.eta else None,
        "items_created": len(created_items),
        "new_products_created": len(new_products),
        "items_skipped": len(skipped_items),
        "created_items": created_items,
        "new_products": new_products,
        "skipped_items": skipped_items,
        "metadata": metadata
    }), 201


def find_product_by_name(db, item_name: str):
    """
    Find a product or child product in the database by matching the QB item name.
    Tries to match against air filter part numbers and stock item names.
    
    Args:
        db: Database session
        item_name: QuickBooks item name to search for
    
    Returns:
        Product or ChildProduct object if found, None otherwise
    """
    if not item_name:
        return None
    
    # First try exact matches for air filters
    air_filter = db.execute(
        select(AirFilter).where(
            or_(
                AirFilter.part_number == item_name,
                func.lower(AirFilter.part_number) == item_name.lower()
            )
        )
    ).first()
    
    # Prefer returning Product over ChildProduct
    if air_filter:
        if air_filter[0].product:
            return air_filter[0].product
        elif air_filter[0].child_product:
            # Return the parent product for child products
            return air_filter[0].child_product.parent_product
    
    # Try stock items
    stock_item = db.execute(
        select(StockItem).where(
            or_(
                StockItem.name == item_name,
                func.lower(StockItem.name) == item_name.lower()
            )
        )
    ).first()
    
    # Prefer returning Product over ChildProduct
    if stock_item:
        if stock_item[0].product:
            return stock_item[0].product
        elif stock_item[0].child_product:
            # Return the parent product for child products
            return stock_item[0].child_product.parent_product
    
    return None
