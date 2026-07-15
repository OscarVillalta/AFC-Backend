import logging

from flask import Blueprint, g, jsonify, request
from flask_jwt_extended import jwt_required
from sqlalchemy import select, func, null
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError, DatabaseError
from sqlalchemy.orm import joinedload
from app.api.Schemas.order_schema import OrderSchema
from database.models import Customer, Supplier, OrderType, OrderStatus, OrderItemType, Transaction, TransactionState, TransactionReason, OUTGOING_TYPES, VALID_ORDER_TYPES
from database.models import Order, OrderItem, Product, AirFilter, StockItem, StockItemCategory, Quantity, OrderTracker, Department, BlockedItem, Media, CalendarSyncStatus
from marshmallow import ValidationError
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Dict, Any, List, Tuple
import re
import requests

from app.config import Config
from app.api.tokens import permission_required
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
    ExternalServiceError,
    InvalidInputError,
)
from app.services.qb_order_service import create_order_from_qb_record, validate_qb_doc_type
from app.services.order_calendar_sync import sync_order_to_calendar, delete_order_calendar_event
from app.services.tracker_import_service import apply_completed_order_state

order_bp = Blueprint("orders", __name__)
order_schema = OrderSchema()
order_list_schema = OrderSchema(many=True)
logger = logging.getLogger(__name__)


def _order_for_calendar_sync(db, order: Order) -> Order:
    return db.execute(
        select(Order)
        .options(joinedload(Order.customer), joinedload(Order.supplier))
        .where(Order.id == order.id)
    ).scalar_one()


def _best_effort_sync_order_calendar(db, order: Order) -> None:
    if not Config.calendar_is_configured():
        return
    try:
        synced_order = _order_for_calendar_sync(db, order)
        event, _ = sync_order_to_calendar(db, synced_order, raise_on_error=False)
        db.commit()
        if event and event.sync_status == CalendarSyncStatus.ERROR.value:
            logger.warning(
                "Calendar sync failed for order %s: %s",
                order.id,
                event.last_error,
            )
    except Exception:
        logger.exception("Calendar sync raised for order %s", order.id)
        db.rollback()


def _best_effort_delete_order_calendar(db, order: Order) -> None:
    try:
        deleted = delete_order_calendar_event(db, order, raise_on_error=False)
        if deleted:
            db.commit()
    except Exception:
        db.rollback()

# GET all orders (paginated, filterable)
@order_bp.route("/orders", methods=["GET"])
@jwt_required()
def get_orders() -> Tuple[Any, int]:
    """
    Retrieve all orders with pagination and optional filters.
    
    Query Parameters:
        page (int): Page number (default: 1)
        limit (int): Items per page (default: 25, max: 100)
        type (str): Filter by order type (specific type or "outgoing" for all customer-facing types)
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
@jwt_required()
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
            "warehouse_id": order.warehouse_id,
            "can_manual_complete": order.can_manual_complete(),
        }), 200
        
    except ResourceNotFoundError as e:
        return jsonify(e.to_dict()), e.status_code
    except CustomValidationError as e:
        return handle_validation_error(e)
    except Exception as e:
        return jsonify({"error": "Unexpected error", "details": str(e)}), 500


# GET order items
@order_bp.route("/orders/<int:order_id>/items", methods=["GET"])
@jwt_required()
def get_order_items(order_id):
    db = g.db
    order = db.get(Order, order_id)

    if not order:
        return jsonify({"error": "Order not found"}), 404

    # Sort items by position
    sorted_items = sorted(order.items, key=lambda x: x.position)

    # Batch fetch pending transaction quantities per order item (avoid N+1 queries)
    pending_by_item: dict = {}
    has_blocking_by_item: set = set()
    has_any_txn_by_item: set = set()
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

        # Items with any pending or committed transaction (blocks no-stock-deduction toggle)
        blocking_rows = db.execute(
            select(Transaction.order_item_id)
            .where(Transaction.order_item_id.in_(item_ids))
            .where(Transaction.state.in_(["pending", "committed"]))
            .distinct()
        ).scalars().all()
        has_blocking_by_item = set(blocking_rows)

        # Items with any transaction at all (blocks deletion)
        any_txn_rows = db.execute(
            select(Transaction.order_item_id)
            .where(Transaction.order_item_id.in_(item_ids))
            .distinct()
        ).scalars().all()
        has_any_txn_by_item = set(any_txn_rows)

    # Batch fetch per-warehouse quantities for all products (avoid N+1 queries)
    product_ids = [item.product_id for item in sorted_items if item.product_id]
    quantities_by_product: dict = {}
    if product_ids:
        from sqlalchemy.orm import joinedload as _joinedload
        qty_rows = db.execute(
            select(Quantity)
            .options(_joinedload(Quantity.warehouse))
            .where(Quantity.product_id.in_(product_ids))
        ).scalars().all()
        for qty in qty_rows:
            wh_name = qty.warehouse.name
            if qty.product_id not in quantities_by_product:
                quantities_by_product[qty.product_id] = {}
            quantities_by_product[qty.product_id][wh_name] = {
                "on_hand": qty.on_hand,
                "reserved": qty.reserved,
                "available": max(qty.on_hand - qty.reserved, 0),
            }

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
            on_hand_by_warehouse = None
            available_by_warehouse = None
        else:
            product = item.product

            if product and product.category.name == "Air Filters":
                part_number = product.air_filter.part_number
            elif product and product.category.name == "Stock Items":
                part_number = product.stock_item.name
            elif product and product.category.name == "Media Items":
                part_number = product.media.part_number
            elif product:
                part_number = f"Product #{product.id}"
            else:
                part_number = "Unknown product"

            # Use the order's warehouse for the primary on_hand/reserved/available fields
            order_wh_qty = None
            if product and item.product_id in quantities_by_product:
                wh_data = quantities_by_product[item.product_id]
                # Find quantity record for the order's own warehouse
                order_wh_name = order.warehouse.name if order.warehouse else None
                if order_wh_name and order_wh_name in wh_data:
                    order_wh_qty = wh_data[order_wh_name]
                elif wh_data:
                    order_wh_qty = next(iter(wh_data.values()))

            on_hand = order_wh_qty["on_hand"] if order_wh_qty else None
            reserved = order_wh_qty["reserved"] if order_wh_qty else None
            available = order_wh_qty["available"] if order_wh_qty else None
            quantity_pending = pending_by_item.get(item.id, 0)
            is_media = product is not None and product.media is not None

            if item.product_id and item.product_id in quantities_by_product:
                wh_data = quantities_by_product[item.product_id]
                on_hand_by_warehouse = {name: data["on_hand"] for name, data in wh_data.items()}
                available_by_warehouse = {name: data["available"] for name, data in wh_data.items()}
            else:
                on_hand_by_warehouse = None
                available_by_warehouse = None

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
            "no_stock_deduction": (
                False
                if order.type == OrderType.INCOMING.value
                else item.no_stock_deduction
            ),
            "on_hand_by_warehouse": on_hand_by_warehouse,
            "available_by_warehouse": available_by_warehouse,
            "has_blocking_transactions": item.id in has_blocking_by_item,
            "has_any_transactions": item.id in has_any_txn_by_item,
        })

    return jsonify(items), 200


# GET serialized order items string
@order_bp.route("/orders/<int:order_id>/serialize", methods=["GET"])
@jwt_required()
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

    # ===============================
    # ❌ Prevent creation of Void orders
    # ===============================
    if data.get("type") == OrderType.VOID.value:
        return jsonify({
            "error": "Cannot create orders with type 'Void'"
        }), 400

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
            warehouse_id=g.active_warehouse_id,
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
    _best_effort_sync_order_calendar(db, order)

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


@order_bp.route("/orders/<int:order_id>/complete-manual", methods=["POST"])
@jwt_required()
@permission_required("orders:edit")
def complete_order_manual(order_id: int) -> Tuple[Any, int]:
    """Mark an order Completed when it has no stock-trackable line items."""
    db = g.db

    try:
        order_id = validate_positive_integer(order_id, "order_id")
        order = db.get(Order, order_id)
        if not order:
            raise ResourceNotFoundError("Order", order_id)

        if not order.can_manual_complete():
            raise InvalidInputError(
                "This order cannot be manually completed. "
                "It must have line items and no stock-trackable products pending fulfillment."
            )

        for item in order.items:
            if item.type in (
                OrderItemType.UNIT_SEPARATOR.value,
                OrderItemType.SECTION_SEPARATOR.value,
            ):
                continue
            if item.skips_inventory():
                item.quantity_fulfilled = item.quantity_ordered

        order.status = OrderStatus.COMPLETED.value
        order.completed_at = datetime.now(timezone.utc)

        safe_commit(db, "manually completing order")

        return jsonify({
            "message": "Order marked as completed.",
            "status": order.status,
            "completed_at": (
                order.completed_at.strftime(Config.DATE_FORMAT)
                if order.completed_at else None
            ),
            "can_manual_complete": False,
        }), 200

    except (ResourceNotFoundError, InvalidInputError, CustomValidationError) as e:
        return jsonify(e.to_dict()), e.status_code
    except IntegrityError as e:
        db.rollback()
        return handle_database_error(e, "manually completing order")
    except DatabaseError as e:
        db.rollback()
        return handle_database_error(e, "manually completing order")
    except Exception as e:
        db.rollback()
        return jsonify({"error": "Unexpected error", "details": str(e)}), 500


@order_bp.route("/orders/<int:order_id>/force-no-stock", methods=["POST"])
@jwt_required()
@permission_required("order:forceNoStock")
def force_order_no_stock(order_id: int) -> Tuple[Any, int]:
    """Set all line items to no-stock-deduction and mark the order completed."""
    db = g.db

    try:
        order_id = validate_positive_integer(order_id, "order_id")
        order = db.execute(
            select(Order).options(joinedload(Order.items)).where(Order.id == order_id)
        ).unique().scalar_one_or_none()
        if not order:
            raise ResourceNotFoundError("Order", order_id)

        if order.type == OrderType.VOID.value:
            raise InvalidInputError("Cannot force no stock on void orders.")
        if order.status == OrderStatus.COMPLETED.value:
            raise InvalidInputError("Order is already completed.")
        if order.type == OrderType.INCOMING.value:
            raise InvalidInputError("No stock deduction is not available on incoming orders.")

        committed_txns = db.scalars(
            select(Transaction)
            .where(Transaction.order_id == order.id)
            .where(Transaction.state == TransactionState.COMMITTED.value)
            .where(Transaction.reason != TransactionReason.ROLLBACK.value)
        ).all()
        if committed_txns:
            raise InvalidInputError(
                "Cannot force no stock while committed inventory transactions exist. "
                "Roll back stock movements first."
            )

        pending_txns = db.scalars(
            select(Transaction)
            .where(Transaction.order_id == order.id)
            .where(Transaction.state == TransactionState.PENDING.value)
        ).all()
        for txn in pending_txns:
            txn.cancel()

        apply_completed_order_state(db, order)

        safe_commit(db, "forcing order no stock completion")

        return jsonify({
            "message": "Order line items set to no stock deduction and marked completed.",
            "status": order.status,
            "completed_at": (
                order.completed_at.strftime(Config.DATE_FORMAT)
                if order.completed_at else None
            ),
            "can_manual_complete": False,
        }), 200

    except (ResourceNotFoundError, InvalidInputError, CustomValidationError) as e:
        return jsonify(e.to_dict()), e.status_code
    except IntegrityError as e:
        db.rollback()
        return handle_database_error(e, "forcing order no stock completion")
    except DatabaseError as e:
        db.rollback()
        return handle_database_error(e, "forcing order no stock completion")
    except Exception as e:
        db.rollback()
        return jsonify({"error": "Unexpected error", "details": str(e)}), 500


@order_bp.route("/orders/<int:order_id>/void", methods=["POST"])
@jwt_required()
def void_order(order_id):
    """
    Void an order by setting its type to VOID and status to VOIDED.
    """
    db = g.db
    order = db.get(Order, order_id)

    if not order:
        return jsonify({"error": "Order not found"}), 404

    if order.type == OrderType.VOID.value:
        return jsonify({"error": "Order is already voided"}), 400

    blocking_txns = db.scalars(
        select(Transaction.id)
        .where(Transaction.order_id == order.id)
        .where(
            or_(
                Transaction.state == TransactionState.PENDING.value,
                and_(
                    Transaction.state == TransactionState.COMMITTED.value,
                    Transaction.reason != TransactionReason.ROLLBACK.value,
                ),
            )
        )
        .limit(1)
    ).first()

    if blocking_txns is not None:
        return jsonify({
            "error": (
                "Order cannot be voided. Cancel pending reservations/orders "
                "and rollback committed stock movements first."
            )
        }), 409

    order.type = OrderType.VOID.value
    order.status = OrderStatus.VOIDED.value

    if order.external_order_number != None:
        order.description = order.description + " Quickbooks ID: " + str(order.external_order_number)
        order.external_order_number = ""
    db.commit()
    _best_effort_sync_order_calendar(db, order)

    return jsonify({
        "message": "Order voided successfully",
        "id": order.id,
        "order_number": order.order_number,
        "type": order.type,
        "status": order.status,
    }), 200


@order_bp.route("/orders/<int:order_id>", methods=["DELETE"])
@permission_required("orders:edit")
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

        _best_effort_delete_order_calendar(db, order)
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

    # ===============================
    # ❌ Prevent modifications to Void orders
    # ===============================
    if order.type == OrderType.VOID.value:
        return jsonify({
            "error": "Cannot modify orders with type 'Void'"
        }), 400

    data = request.get_json() or {}
    previous_type = order.type
    previous_eta = order.eta
    previous_customer_id = order.customer_id
    previous_supplier_id = order.supplier_id

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
    should_sync_calendar = (
        ("type" in data and order.type != previous_type)
        or ("eta" in data and order.eta != previous_eta)
        or ("cs_id" in data and (
            order.customer_id != previous_customer_id
            or order.supplier_id != previous_supplier_id
        ))
        or ("supplier_id" in data and order.supplier_id != previous_supplier_id)
    )
    if should_sync_calendar:
        _best_effort_sync_order_calendar(db, order)

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

def _parse_search_list_arg(name: str) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for raw in request.args.getlist(name):
        for part in raw.split(","):
            value = part.strip()
            if value and value not in seen:
                seen.add(value)
                values.append(value)
    return values


def _parse_search_int_list_arg(name: str) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for raw in _parse_search_list_arg(name):
        try:
            parsed = int(raw)
        except ValueError:
            continue
        if parsed not in seen:
            seen.add(parsed)
            ids.append(parsed)
    return ids


@order_bp.route("/orders/search", methods=["GET"])
@jwt_required()
def search_orders():
    db = g.db

    search = request.args.get("search")
    order_number = request.args.get("order_number")
    external_order_number = request.args.get("external_order_number")
    description = request.args.get("description")
    customer_name = request.args.get("customer_name")
    supplier_name = request.args.get("supplier_name")
    customer_ids = _parse_search_int_list_arg("customer_id")
    supplier_ids = _parse_search_int_list_arg("supplier_id")
    order_types = _parse_search_list_arg("type")
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

    # Scope to the active warehouse
    filters.append(Order.warehouse_id == g.active_warehouse_id)

    if order_types:
        if len(order_types) == 1 and order_types[0].lower() == "outgoing":
            filters.append(Order.type.in_(OUTGOING_TYPES))
        else:
            normalized_types = [
                t for t in order_types if t.lower() not in {"all", "outgoing"}
            ]
            if normalized_types:
                filters.append(Order.type.in_(normalized_types))

    if status and status.lower() != "all":
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

    # Customer / supplier filters (exact ID match; OR when both are provided)
    party_conds = []
    if customer_ids:
        party_conds.append(Order.customer_id.in_(customer_ids))
    if supplier_ids:
        party_conds.append(Order.supplier_id.in_(supplier_ids))
    if party_conds:
        filters.append(or_(*party_conds))

    # Legacy name-based filters
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
        query.order_by(Order.order_number.desc())
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
        if item.skips_inventory():
            continue

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
    """
    db = g.db
    data = request.get_json() or {}

    reference_number = data.get("reference_number")
    qb_doc_type = data.get("qb_doc_type", "").lower()
    order_type_override = data.get("order_type")

    if not reference_number:
        return jsonify({"error": "reference_number is required"}), 400

    if not isinstance(reference_number, str) or not reference_number.strip():
        return jsonify({"error": "reference_number must be a non-empty string"}), 400

    reference_number = reference_number.strip()

    try:
        validate_qb_doc_type(qb_doc_type)
    except CustomValidationError as e:
        return jsonify({"error": str(e)}), 400

    try:
        result = create_order_from_qb_record(
            db,
            reference_number=reference_number,
            qb_doc_type=qb_doc_type,
            order_type=order_type_override,
            warehouse_id=g.active_warehouse_id,
        )
    except DuplicateResourceError as e:
        return jsonify(e.to_dict()), e.status_code
    except ExternalServiceError as e:
        return jsonify(e.to_dict()), e.status_code
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
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
        return jsonify({
            "error": "Failed to create order from QuickBooks",
            "details": str(e),
        }), 500

    order = result.order
    _best_effort_sync_order_calendar(db, order)

    return jsonify({
        "message": "Order created successfully from QuickBooks",
        "order_id": order.id,
        "order_number": order.order_number,
        "external_order_number": order.external_order_number,
        "customer_name": result.customer.name if result.customer else None,
        "vendor_name": result.supplier.name if result.supplier else None,
        "eta": order.eta.strftime("%Y-%m-%d") if order.eta else None,
        "items_created": len(result.created_items),
        "new_products_created": len(result.new_products),
        "items_skipped": len(result.skipped_items),
        "created_items": result.created_items,
        "new_products": result.new_products,
        "skipped_items": result.skipped_items,
        "metadata": result.metadata,
    }), 201


def find_product_by_name(db, item_name: str):
    """Re-export for backward compatibility."""
    from app.services.qb_order_service import find_product_by_name as _find
    return _find(db, item_name)