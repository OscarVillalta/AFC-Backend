from flask import Blueprint, g, jsonify, request
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError, DatabaseError
from app.api.Schemas.order_item_schema import OrderItemSchema
from database.models import OrderItem, Order, Product, ChildProduct, Transaction, TransactionState, OrderType, OrderItemType, Quantity
from marshmallow import ValidationError
from typing import Tuple, Any

from app.api.validation import (
    validate_positive_integer,
    validate_string,
    ValidationError as CustomValidationError
)
from app.api.error_handling import (
    handle_database_error,
    handle_validation_error,
    safe_commit,
    ResourceNotFoundError,
    InvalidInputError
)

order_item_bp = Blueprint("order_items", __name__)
item_schema = OrderItemSchema()
item_list_schema = OrderItemSchema(many=True)

# GET a single order item
@order_item_bp.route("/order_items/<int:item_id>", methods=["GET"])
def get_order_item(item_id: int) -> Tuple[Any, int]:
    """
    Retrieve a single order item by ID.
    
    Args:
        item_id: The ID of the order item
    
    Returns:
        JSON response with order item details
    """
    db = g.db
    
    try:
        item_id = validate_positive_integer(item_id, "item_id")
        item = db.get(OrderItem, item_id)
        
        if not item:
            raise ResourceNotFoundError("Order item", item_id)
        
        return jsonify(item_schema.dump(item)), 200
        
    except ResourceNotFoundError as e:
        return jsonify(e.to_dict()), e.status_code
    except CustomValidationError as e:
        return handle_validation_error(e)
    except Exception as e:
        return jsonify({"error": "Unexpected error", "details": str(e)}), 500

@order_item_bp.route("/order_items", methods=["POST"])
def create_order_item() -> Tuple[Any, int]:
    """
    Create a new order item.
    
    Request Body:
        order_id (int): ID of the parent order
        product_id (int): ID of the product (not required for separators)
        child_product_id (int): ID of the child product (alternative to product_id)
        type (str): Item type - Unit_Separator (visual unit separator), Section_Separator
            (section header), Product_Item (regular product line item), or Sales_Item
            (non-inventory sales line item, reserved for future use)
        quantity_ordered (int): Quantity ordered
        note (str): Optional note
        position (int): Optional position in the order
    
    Returns:
        JSON response with created order item
    """
    db = g.db
    json_data = request.get_json() or {}
    
    try:
        # Validate via Marshmallow
        data = item_schema.load(json_data)
        
        # Business integrity checks
        order = db.get(Order, data["order_id"])
        if not order:
            raise ResourceNotFoundError("Order", data["order_id"])
        
        item_type = data.get("type", OrderItemType.PRODUCT_ITEM.value)
        is_separator = item_type in (OrderItemType.UNIT_SEPARATOR.value, OrderItemType.SECTION_SEPARATOR.value)
        
        # Separator items don't need a product
        if is_separator:
            if not data.get("note"):
                raise InvalidInputError("Separator items must have a note/description")
            product_id = None
            child_product_id = None
            quantity_ordered = 0
        else:
            # Check for product_id or child_product_id
            has_product = "product_id" in data and data["product_id"] is not None
            has_child_product = "child_product_id" in data and data["child_product_id"] is not None
            
            if not has_product and not has_child_product:
                raise InvalidInputError("Either product_id or child_product_id is required for non-separator items")
            
            if has_product and has_child_product:
                raise InvalidInputError("Cannot specify both product_id and child_product_id")
            
            if has_product:
                product = db.get(Product, data["product_id"])
                if not product:
                    raise ResourceNotFoundError("Product", data["product_id"])
                product_id = product.id
                child_product_id = None
                # Auto-detect media products and set type to Media_Cut
                if product.category and product.category.name == "Media":
                    item_type = OrderItemType.MEDIA_CUT.value
            else:
                child_product = db.get(ChildProduct, data["child_product_id"])
                if not child_product:
                    raise ResourceNotFoundError("ChildProduct", data["child_product_id"])
                product_id = None
                child_product_id = child_product.id
                # Auto-detect media child products and set type to Media_Cut
                if child_product.category and child_product.category.name == "Media":
                    item_type = OrderItemType.MEDIA_CUT.value
            
            quantity_ordered = validate_positive_integer(
                data["quantity_ordered"], 
                "quantity_ordered", 
                allow_zero=True
            )
        
        # Handle position
        position = data.get("position")
        if position is None:
            # If no position specified, add at the end
            max_position_result = db.execute(
                select(func.max(OrderItem.position))
                .where(OrderItem.order_id == order.id)
            ).scalar()
            position = (max_position_result or -1) + 1
        else:
            position = validate_positive_integer(position, "position", allow_zero=True)
            # If position is specified, shift existing items down
            # Use SQLAlchemy 2.0 style update
            from sqlalchemy import update
            db.execute(
                update(OrderItem)
                .where(OrderItem.order_id == order.id)
                .where(OrderItem.position >= position)
                .values(position=OrderItem.position + 1)
            )
        
        # Create OrderItem
        item = OrderItem(
            order_id=order.id,
            product_id=product_id,
            child_product_id=child_product_id,
            type=item_type,
            quantity_ordered=quantity_ordered,
            quantity_fulfilled=0,
            note=data.get("note"),
            position=position,
        )
        
        db.add(item)
        safe_commit(db, "creating order item")
        
        return jsonify(item_schema.dump(item)), 201
        
    except ValidationError as err:
        return jsonify({"error": "Validation failed", "details": err.messages}), 400
    except (ResourceNotFoundError, InvalidInputError, CustomValidationError) as e:
        return jsonify(e.to_dict()), e.status_code
    except IntegrityError as e:
        db.rollback()
        return handle_database_error(e, "creating order item")
    except DatabaseError as e:
        db.rollback()
        return handle_database_error(e, "creating order item")
    except Exception as e:
        db.rollback()
        return jsonify({"error": "Unexpected error", "details": str(e)}), 500

# 🔹 PATCH: Update general info (quantity/note)
@order_item_bp.route("/order_items/<int:item_id>", methods=["PATCH"])
def update_order_item(item_id):
    db = g.db
    item = db.get(OrderItem, item_id)
    if not item:
        return jsonify({"error": "Order item not found"}), 404

    data = request.get_json()
    if "quantity_ordered" in data:
        item.quantity_ordered = data["quantity_ordered"]
    if "note" in data:
        item.note = data["note"]
    if "type" in data:
        new_type = data["type"]
        valid_types = [t.value for t in OrderItemType]
        if new_type not in valid_types:
            return jsonify({"error": f"Invalid type. Must be one of: {valid_types}"}), 400
        # Allow toggling between separator types
        separator_types = [OrderItemType.UNIT_SEPARATOR.value, OrderItemType.SECTION_SEPARATOR.value]
        # Allow toggling between product item types (Product_Item <-> Media_Cut)
        product_item_types = [OrderItemType.PRODUCT_ITEM.value, OrderItemType.MEDIA_CUT.value]
        if item.type in separator_types and new_type in separator_types:
            item.type = new_type
        elif item.type in product_item_types and new_type in product_item_types:
            item.type = new_type
        else:
            return jsonify({"error": "Type change is only allowed between separator types or between Product_Item and Media_Cut"}), 400

    db.commit()
    return jsonify({
        "message": "Order item updated successfully.",
        "item": item_schema.dump(item)
    }), 200

@order_item_bp.route("/order_items/<int:item_id>/allocate", methods=["POST"])
def allocate_order_item(item_id):
    db = g.db
    item = db.get(OrderItem, item_id)
    if not item:
        return jsonify({"error": "Order item not found"}), 404

    order = item.order
    if not order:
        return jsonify({"error": "Order not associated"}), 400

    # 🔹 Parse requested allocation quantity
    body = request.get_json(silent=True) or {}
    qty_to_allocate = body.get("quantity")

    try:
        qty_to_allocate = int(qty_to_allocate)
    except (ValueError, TypeError):
        return jsonify({"error": "Quantity must be an integer."}), 400

    if qty_to_allocate is None or qty_to_allocate <= 0:
        return jsonify({"error": "A positive 'quantity' is required for allocation."}), 400

    note = body.get("note")

    # 🔹 Determine direction and reason
    sign = 1 if order.type == "supplier" else -1
    qty_delta = qty_to_allocate * sign
    reason = "receive" if order.type == "supplier" else "shipment"

    # 🔹 Compute total already allocated (pending + committed)
    # Use absolute value of quantity_delta since transactions can be positive or negative
    existing_qty_abs = abs(db.scalar(
        select(func.coalesce(func.sum(Transaction.quantity_delta), 0))
        .where(Transaction.order_item_id == item.id)
        .where(Transaction.state.in_(["pending", "committed", "rolled_back"]))
    )) or 0

    projected_total = existing_qty_abs + qty_to_allocate

    # 🔹 Prevent over-allocation
    if projected_total > item.quantity_ordered:
        return jsonify({
            "error": "Allocation exceeds quantity ordered.",
            "ordered": item.quantity_ordered,
            "allocated_so_far": existing_qty_abs,
            "attempted_allocation": qty_to_allocate
        }), 400

    # 🔹 Create pending transaction
    txn = Transaction(
        product_id=item.product_id,
        child_product_id=item.child_product_id,
        order_id=order.id,
        order_item_id=item.id,
        warehouse_id=order.warehouse_id,
        quantity_delta=qty_delta,
        reason=reason,
        state="pending",
        note=note or None
    )

    db.add(txn)
    order.update_status()
    db.commit()

    return jsonify({
        "message": f"Allocated {qty_to_allocate} unit(s) for order item {item_id}.",
        "transaction": txn.to_dict(),
        "order_status": order.status
    }), 201

@order_item_bp.route("/order_items/<int:item_id>/allocate_remaining", methods=["POST"])
def allocate_remaining_order_items(item_id):
    db = g.db
    item = db.get(OrderItem, item_id)
    if not item:
        return jsonify({"error": "Order item not found"}), 404

    order = item.order
    if not order:
        return jsonify({"error": "Order not associated"}), 400
    
    body = request.get_json(silent=True) or {}
    note = body.get("note")

    qty_to_fulfill = item.quantity_ordered - item.quantity_fulfilled

    if qty_to_fulfill < 0:
        return jsonify({
            "error": "There is more fulfilled than ordered already.",
            "ordered": item.quantity_ordered,
            "fulfilled_so_far": item.quantity_fulfilled,
        }), 400

    if qty_to_fulfill == 0:
        return jsonify({
            "message": "Order item is already fully fulfilled.",
        }), 203

    # 🔹 MEDIA_CUT bypass: skip inventory transaction, directly mark as fulfilled
    if item.type == OrderItemType.MEDIA_CUT.value:
        item.quantity_fulfilled += qty_to_fulfill
        item.quantity_fulfilled = max(0, min(item.quantity_fulfilled, item.quantity_ordered))
        order.update_status()
        db.commit()
        return jsonify({
            "message": f"Fulfilled {qty_to_fulfill} unit(s) for media cut order item {item_id} (no stock deduction).",
            "order_status": order.status
        }), 200

    sign = 1 if order.type == "incoming" else -1
    reason = "receive" if order.type == "incoming" else "shipment"
    
    existing_qty = db.scalar(
        select(func.coalesce(func.sum(Transaction.quantity_delta), 0))
        .where(Transaction.order_item_id == item.id)
        .where(Transaction.state.in_(["pending", "committed", "rolled_back"]))
    ) or 0

    qty_to_allocate = item.quantity_ordered - abs(existing_qty)

    # 🔹 Prevent over-allocation
    if qty_to_allocate < 0:
        return jsonify({
            "error": "There is more allocated than ordered already.",
            "ordered": item.quantity_ordered,
            "allocated_so_far": existing_qty,
            "attempted_allocation": qty_to_allocate
        }), 400
    
    if qty_to_allocate == 0:
        return jsonify({
            "message": "Order item is already fully allocated.",
        }), 203

    # 🔹 Create pending transaction
    txn = Transaction(
        product_id=item.product_id,
        child_product_id=item.child_product_id,
        order_id=order.id,
        order_item_id=item.id,
        warehouse_id=order.warehouse_id,
        quantity_delta= qty_to_allocate * sign,
        reason=reason,
        state="pending",
        note=note or None
    )

    # Get the quantity record scoped to the order's warehouse
    product_id_for_qty = item.product_id
    if product_id_for_qty is None and item.child_product:
        product_id_for_qty = item.child_product.parent_product_id

    qty_record = None
    if product_id_for_qty:
        qty_record = db.execute(
            select(Quantity).where(
                (Quantity.product_id == product_id_for_qty) &
                (Quantity.warehouse_id == order.warehouse_id)
            )
        ).scalar_one_or_none()

    if not qty_record:
        return jsonify({"error": "Quantity record not found"}), 404
    
    if order.type == "incoming":
        qty_record.ordered += qty_to_allocate
    else:
        qty_record.reserved += qty_to_allocate

    db.add(txn)  
    db.commit()

    return jsonify({
        "message": f"Allocated {qty_to_allocate} unit(s) for order item {item_id}.",
        "transaction": txn.to_dict(),
        "order_status": order.status
    }), 201

@order_item_bp.route("/order_items/<int:item_id>/commit", methods=["PATCH"])
def commit_single_order_item_txn(item_id: int) -> Tuple[Any, int]:
    """
    Commit a pending transaction for an order item.
    
    Args:
        item_id: The ID of the order item
    
    Request Body:
        txn_id (int): The ID of the transaction to commit
    
    Returns:
        JSON response with committed transaction details
    """
    db = g.db
    
    try:
        # Validate item_id
        item_id = validate_positive_integer(item_id, "item_id")
        
        item = db.get(OrderItem, item_id)
        if not item:
            raise ResourceNotFoundError("Order item", item_id)
        
        order = item.order
        if not order:
            raise InvalidInputError("Order item is not associated with an order")
        
        # Extract txn_id from request body
        data = request.get_json(silent=True) or {}
        if "txn_id" not in data:
            raise InvalidInputError("txn_id is required in request body")
        
        txn_id = validate_positive_integer(data["txn_id"], "txn_id")
        
        txn = db.get(Transaction, txn_id)
        if not txn:
            raise ResourceNotFoundError("Transaction", txn_id)
        
        if txn.order_item_id != item.id:
            raise InvalidInputError("Transaction does not belong to this order item")
        
        if txn.state != "pending":
            return jsonify({
                "message": "Transaction already committed or cancelled",
                "transaction": txn.to_dict()
            }), 200
        
        # Perform the commit
        txn.commit(db)
        order.update_status()
        safe_commit(db, "committing transaction")
        
        return jsonify({
            "message": f"Transaction {txn.id} committed successfully for order item {item_id}.",
            "transaction": txn.to_dict(),
            "order_item": item.to_dict(),
            "order_status": order.status
        }), 200
        
    except (ResourceNotFoundError, InvalidInputError, CustomValidationError) as e:
        return jsonify(e.to_dict()), e.status_code
    except ValueError as e:
        db.rollback()
        return jsonify({"error": f"Validation error: {str(e)}"}), 400
    except IntegrityError as e:
        db.rollback()
        return handle_database_error(e, "committing transaction")
    except DatabaseError as e:
        db.rollback()
        return handle_database_error(e, "committing transaction")
    except Exception as e:
        db.rollback()
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


@order_item_bp.route("/order_items/<int:item_id>/commit_all", methods=["PATCH"])
def commit_all_order_item_txns(item_id):
    db = g.db

    try:
        item = db.get(OrderItem, item_id)
        if not item:
            return jsonify({"error": "Order item not found"}), 404

        order = item.order
        if not order:
            return jsonify({"error": "Order not associated"}), 400

        # 🔹 Gather all pending transactions for this line item
        pending_txns = db.scalars(
            select(Transaction)
            .where(Transaction.order_item_id == item.id)
            .where(Transaction.state == "pending")
        ).all()

        if not pending_txns:
            return jsonify({"message": "No pending transactions to commit."}), 200

        committed = 0

        if order.to_dict()["type"] == "incoming":
            for txn in pending_txns:
                txn.commit(db)
        else:
            for txn in pending_txns: 
                qty_record = txn._get_quantity_record()
                qty = abs(txn.quantity_delta)

                if qty > qty_record.on_hand:
                    return jsonify({ "error": f"Not enough inventory. On hand: {qty_record.on_hand}, required: {qty}"}), 409
            
                txn.commit(db)
                committed += 1

        order.update_status()
        db.commit()
        

        return jsonify({
            "message": f"Committed {committed} pending transaction(s) for order item {item_id}.",
            "committed_txn_ids": [t.id for t in pending_txns],
            "order_item": item.to_dict(),
            "order_status": order.status
        }), 200

    except (ResourceNotFoundError, InvalidInputError, CustomValidationError) as e:
        return jsonify(e.to_dict()), e.status_code
    except ValueError as e:
        db.rollback()
        return jsonify({"error": f"Validation error: {str(e)}"}), 400
    except IntegrityError as e:
        db.rollback()
        return handle_database_error(e, "committing transaction")
    except DatabaseError as e:
        db.rollback()
        return handle_database_error(e, "committing transaction")
    except Exception as e:
        db.rollback()
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


@order_item_bp.route("/order_items/<int:item_id>/transactions", methods=["GET"])
def get_order_item_transactions(item_id):
    db = g.db
    item = db.get(OrderItem, item_id)

    if not item:
        return jsonify({"error": "Order item not found"}), 404

    txns = (
        db.query(Transaction)
        .filter(Transaction.order_item_id == item.id)
        .order_by(Transaction.created_at.desc())
        .all()
    )

    return jsonify([
        {
            "id": t.id,
            "quantity_delta": t.quantity_delta,
            "reason": t.reason,
            "state": t.state,
            "note": t.note,
            "created_at": t.created_at.isoformat(),
        }
        for t in txns
    ]), 200

@order_item_bp.route(
    "/order-items/<int:order_item_id>/transactions",
    methods=["POST"]
)
def create_order_item_transaction(order_item_id):
    db = g.db
    data = request.get_json() or {}

    # -------------------------
    # Required fields
    # -------------------------
    quantity = data.get("quantity")
    reason = data.get("reason")
    note = data.get("note")

    if not quantity or quantity <= 0:
        return jsonify({
            "error": "quantity must be a positive number"
        }), 400

    if not reason:
        return jsonify({
            "error": "reason is required"
        }), 400

    # -------------------------
    # Load order item
    # -------------------------
    item = db.get(OrderItem, order_item_id)
    if not item:
        return jsonify({"error": "Order item not found"}), 404

    order = item.order
    product = item.product
    child_product = item.child_product

    # Get the quantity record scoped to the order's warehouse
    product_id_for_qty = product.id if product else (child_product.parent_product_id if child_product else None)
    qty = db.execute(
        select(Quantity).where(
            (Quantity.product_id == product_id_for_qty) &
            (Quantity.warehouse_id == order.warehouse_id)
        )
    ).scalar_one_or_none() if product_id_for_qty else None

    if not qty:
        return jsonify({
            "error": "Quantity record missing"
        }), 400

    remaining = item.quantity_ordered - item.quantity_fulfilled

    if quantity > remaining:
        return jsonify({
            "error": "Quantity exceeds remaining order item amount"
        }), 400

    # -------------------------
    # Create PENDING transaction
    # -------------------------
    txn = Transaction(
        product_id=product.id if product else None,
        child_product_id=child_product.id if child_product else None,
        order_id=order.id,
        order_item_id=item.id,
        warehouse_id=order.warehouse_id,
        quantity_delta=quantity,
        reason=reason,
        note=note,
        state=TransactionState.PENDING.value,
    )

    # -------------------------
    # Apply PENDING inventory effects
    # -------------------------
    if order.type == OrderType.OUTGOING.value:
        # Reserve stock
        if qty.on_hand < quantity:
            return jsonify({
                "error": "Insufficient on-hand stock to reserve"
            }), 400

        qty.reserved += quantity

    elif order.type == OrderType.INCOMING.value:
        # Mark as ordered
        qty.ordered += quantity

    db.add(txn)
    db.commit()

    return jsonify({
        "id": txn.id,
        "state": txn.state,
        "quantity": quantity,
        "reason": txn.reason,
        "note": txn.note,
        "created_at": txn.created_at.isoformat(),
    }), 201

@order_item_bp.route("/order_items/<int:item_id>", methods=["DELETE"])
def delete_order_item(item_id):
    db = g.db
    item = db.get(OrderItem, item_id)

    if not item:
        return jsonify({"error": "Item not found"}), 404

    if item.transactions and len(item.transactions) > 0:
        return jsonify({
            "error": "Cannot delete item with transactions."
        }), 400

    db.delete(item)
    db.commit()

    return jsonify({"success": True}), 200


@order_item_bp.route("/orders/<int:order_id>/items/reorder", methods=["PATCH"])
def reorder_order_items(order_id):
    """
    Reorder items in an order.
    Expects JSON body with:
    {
        "item_id": <id of item to move>,
        "new_position": <new position index>
    }
    """
    db = g.db
    data = request.get_json() or {}

    item_id = data.get("item_id")
    new_position = data.get("new_position")

    if item_id is None or new_position is None:
        return jsonify({"error": "item_id and new_position are required"}), 400

    # Verify order exists
    order = db.get(Order, order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    # Verify item exists and belongs to this order
    item = db.get(OrderItem, item_id)
    if not item:
        return jsonify({"error": "Item not found"}), 404
    
    if item.order_id != order_id:
        return jsonify({"error": "Item does not belong to this order"}), 400

    old_position = item.position

    if old_position == new_position:
        return jsonify({"message": "Item already in that position"}), 200

    # Update positions
    if old_position < new_position:
        # Moving down: shift items between old and new position up
        db.query(OrderItem).filter(
            OrderItem.order_id == order_id,
            OrderItem.position > old_position,
            OrderItem.position <= new_position
        ).update(
            {OrderItem.position: OrderItem.position - 1},
            synchronize_session=False
        )
    else:
        # Moving up: shift items between new and old position down
        db.query(OrderItem).filter(
            OrderItem.order_id == order_id,
            OrderItem.position >= new_position,
            OrderItem.position < old_position
        ).update(
            {OrderItem.position: OrderItem.position + 1},
            synchronize_session=False
        )

    # Update the item's position
    item.position = new_position
    db.commit()

    return jsonify({
        "message": "Item reordered successfully",
        "item_id": item_id,
        "old_position": old_position,
        "new_position": new_position
    }), 200


