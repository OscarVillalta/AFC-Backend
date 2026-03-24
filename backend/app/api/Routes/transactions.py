from flask import abort, g, jsonify, request, Blueprint
from sqlalchemy import select, func, or_, text
from database.models import (
    Quantity,
    Transaction,
    Product,
    ChildProduct,
    TransactionState,
    OrderType,
    Order,
    OrderItem,
    AirFilter,
    Conversion,
    ConversionBatch,
    ConversionDecrease,
    ConversionState,
)
from datetime import datetime, timezone
from app.api.Schemas.transaction_schema import TransactionSchema

class InventoryConflictError(Exception):
    pass

transaction_bp = Blueprint("transactions", __name__)
txn_schema = TransactionSchema()
txn_list_schema = TransactionSchema(many=True)


def validate_product_or_child_product_exclusive(data):
    """Validate that either product_id or child_product_id is provided, but not both"""
    has_product = "product_id" in data and data["product_id"] is not None
    has_child = "child_product_id" in data and data["child_product_id"] is not None
    
    if not has_product and not has_child:
        return {"error": "Either product_id or child_product_id is required"}, 400
    
    if has_product and has_child:
        return {"error": "Cannot specify both product_id and child_product_id"}, 400
    
    return None


def _get_entity_and_quantity(db, product_id=None, child_product_id=None, warehouse_id=None):
    """
    Helper to fetch a product/child_product and its quantity record for the given warehouse.
    """
    if product_id:
        product = db.get(Product, product_id)
        if not product:
            return None, None, {"error": "Product not found"}, 404
        qty = db.execute(
            select(Quantity).where(
                (Quantity.product_id == product_id) &
                (Quantity.warehouse_id == warehouse_id)
            )
        ).scalar_one_or_none()
        if not qty:
            return None, None, {"error": "Quantity record not found for product in this warehouse"}, 404
        return product, qty, None, None

    if child_product_id:
        child_product = db.get(ChildProduct, child_product_id)
        if not child_product:
            return None, None, {"error": "Child product not found"}, 404
        qty = db.execute(
            select(Quantity).where(
                (Quantity.product_id == child_product.parent_product_id) &
                (Quantity.warehouse_id == warehouse_id)
            )
        ).scalar_one_or_none()
        if not qty:
            return None, None, {"error": "Parent product's quantity record not found in this warehouse"}, 404
        return child_product, qty, None, None

    return None, None, {"error": "Either product_id or child_product_id is required"}, 400


@transaction_bp.route("/transactions", methods=["GET"])
def get_transactions():
    db = g.db
    page = request.args.get("page", default=1, type=int)
    limit = request.args.get("limit", default=25, type=int)
    offset = (page - 1) * limit

    query = select(Transaction).offset(offset).limit(limit).order_by(Transaction.id.desc())
    txns = db.execute(query).scalars().all()

    total_count = db.execute(
    select(func.count()).select_from(Transaction)
    ).scalar()

    return jsonify({
        "page": page,
        "limit": limit,
        "total": total_count,
        "results": txn_list_schema.dump(txns)
    }), 200

@transaction_bp.route("/transactions/<int:txn_id>", methods=["GET"])
def get_transaction(txn_id):
    db = g.db
    txn = db.get(Transaction, txn_id)
    if not txn:
        return jsonify({"error": "Transaction not found"}), 404
    return jsonify(txn_schema.dump(txn)), 200


@transaction_bp.route("/transactions/<int:txn_id>/on_hand", methods=["GET"])
def get_transaction_on_hand(txn_id):
    """
    For a committed transaction with a ledger_sequence, compute the on_hand
    stock before and after this transaction was applied by summing all
    committed quantity_deltas for the same product up to this ledger_sequence.
    """
    db = g.db
    txn = db.get(Transaction, txn_id)
    warehouse_id = g.active_warehouse_id
    if not txn:
        return jsonify({"error": "Transaction not found"}), 404

    if txn.state != "committed" or txn.ledger_sequence is None:
        return jsonify({"error": "On-hand data is only available for committed transactions with a ledger sequence"}), 400

    # Determine the product filter
    if txn.product_id is not None:
        product_filter = Transaction.product_id == txn.product_id
    elif txn.child_product_id is not None:
        product_filter = Transaction.child_product_id == txn.child_product_id
    else:
        return jsonify({"error": "Transaction has no associated product"}), 400

    # Sum all committed deltas for the same product with ledger_sequence <= this transaction's sequence
    on_hand_after_query = (
        select(func.coalesce(func.sum(Transaction.quantity_delta), 0))
        .where(
            product_filter,
            or_(Transaction.state == "committed", Transaction.state == "rolled_back"),
            Transaction.warehouse_id == warehouse_id,
            Transaction.ledger_sequence.isnot(None),
            Transaction.ledger_sequence <= txn.ledger_sequence,
        )
    )
    on_hand_after = db.execute(on_hand_after_query).scalar()
    on_hand_before = on_hand_after - txn.quantity_delta

    return jsonify({
        "transaction_id": txn.id,
        "on_hand_before": on_hand_before,
        "on_hand_after": on_hand_after,
    }), 200

@transaction_bp.route("/transactions/summary", methods=["GET"])
def get_transaction_summary():
    """Return summary stats for filtered transactions (used by summary bar)."""
    db = g.db

    # Reuse same filter logic as search
    product_name = request.args.get("product_name", type=str)
    order_id = request.args.get("order_id", type=int)
    state = request.args.get("state")
    reason = request.args.get("reason", type=str)
    note = request.args.get("note", type=str)
    start_date = request.args.get("start_date", type=str)
    end_date = request.args.get("end_date", type=str)
    before_date = request.args.get("before_date", type=str)
    after_date = request.args.get("after_date", type=str)

    filters = []

    #warehouse scope
    warehouse_id = g.active_warehouse_id
    print("Active warehouse ID:", warehouse_id)
    filters.append(Transaction.warehouse_id == warehouse_id)

    if product_name:
        AirFilter_subquery = select(AirFilter.id).where(
            AirFilter.part_number.ilike(f"%{product_name}%")
        )
        product_subquery = select(Product.id).where(
            Product.reference_id.in_(AirFilter_subquery)
        )
        child_product_subquery = select(ChildProduct.id).where(
            ChildProduct.reference_id.in_(AirFilter_subquery)
        )
        filters.append(
            or_(
                Transaction.product_id.in_(product_subquery),
                Transaction.child_product_id.in_(child_product_subquery)
            )
        )

    if order_id:
        filters.append(Transaction.order_id == order_id)
    if state:
        filters.append(Transaction.state == state)
    if reason:
        filters.append(Transaction.reason.ilike(f"%{reason}%"))
    if note:
        filters.append(Transaction.note.ilike(f"%{note}%"))

    try:
        if start_date and end_date:
            filters.append(Transaction.created_at >= datetime.fromisoformat(start_date))
            filters.append(Transaction.created_at <= datetime.fromisoformat(end_date))
        elif before_date:
            filters.append(Transaction.created_at <= datetime.fromisoformat(before_date))
        elif after_date:
            filters.append(Transaction.created_at >= datetime.fromisoformat(after_date))
    except ValueError:
        return jsonify({"error": "Invalid date format. Use ISO format (YYYY-MM-DD)."}), 400

    # Total count
    total_q = select(func.count()).select_from(Transaction)
    if filters:
        total_q = total_q.where(*filters)
    total = db.execute(total_q).scalar() or 0

    # Net quantity change
    net_q = select(func.coalesce(func.sum(Transaction.quantity_delta), 0))
    if filters:
        net_q = net_q.where(*filters)
    net_qty = db.execute(net_q).scalar()

    # Committed count
    committed_q = select(func.count()).select_from(Transaction).where(
        Transaction.state == "committed"
    )
    if filters:
        committed_q = committed_q.where(*filters)
    committed = db.execute(committed_q).scalar() or 0

    # Pending count
    pending_q = select(func.count()).select_from(Transaction).where(
        Transaction.state == "pending"
    )
    if filters:
        pending_q = pending_q.where(*filters)
    pending = db.execute(pending_q).scalar() or 0

    return jsonify({
        "total": total,
        "net_quantity_change": net_qty,
        "committed_count": committed,
        "pending_count": pending,
    }), 200


@transaction_bp.route("/transactions/search", methods=["GET"])
def filter_transactions():
    db = g.db

    # --- Filters
    product_id = request.args.get("product_id", type=int)
    child_product_id = request.args.get("child_product_id", type=int)
    product_name = request.args.get("product_name", type=str)
    order_id = request.args.get("order_id", type=int)
    state = request.args.get("state")
    reason = request.args.get("reason", type=str)
    note = request.args.get("note", type=str)
    
    # Date filters
    start_date = request.args.get("start_date", type=str)
    end_date = request.args.get("end_date", type=str)
    before_date = request.args.get("before_date", type=str)
    after_date = request.args.get("after_date", type=str)

    # --- Pagination
    page = request.args.get("page", default=1, type=int)
    limit = request.args.get("limit", default=25, type=int)
    offset = (page - 1) * limit

    # --- Build base query
    query = select(Transaction)
    filters = []

    # Scope to the active warehouse
    warehouse_id = g.active_warehouse_id
    filters.append(Transaction.warehouse_id == warehouse_id)

    if product_id:
        filters.append(Transaction.product_id == product_id)
    
    if child_product_id:
        filters.append(Transaction.child_product_id == child_product_id)
    
    # Search by product name (partial match) - check both Product and ChildProduct
    if product_name:
        AirFilter_subquery = select(AirFilter.id).where(
            AirFilter.part_number.ilike(f"%{product_name}%")
        )

        # Products with matching air filters
        product_subquery = select(Product.id).where(
            Product.reference_id.in_(AirFilter_subquery)
        )
        
        # Child products with matching air filters
        child_product_subquery = select(ChildProduct.id).where(
            ChildProduct.reference_id.in_(AirFilter_subquery)
        )

        filters.append(
            or_(
                Transaction.product_id.in_(product_subquery),
                Transaction.child_product_id.in_(child_product_subquery)
            )
        )
    
    if order_id:
        filters.append(Transaction.order_id == order_id)
    if state:
        filters.append(Transaction.state == state)
    
    # Filter by reason (partial match, case insensitive)
    if reason:
        filters.append(Transaction.reason.ilike(f"%{reason}%"))
    
    # Filter by note (partial match, case insensitive)
    if note:
        filters.append(Transaction.note.ilike(f"%{note}%"))
    
    # Date filters
    try:
        if start_date and end_date:
            # Between two dates (inclusive)
            filters.append(Transaction.created_at >= datetime.fromisoformat(start_date))
            filters.append(Transaction.created_at <= datetime.fromisoformat(end_date))
        elif before_date:
            # Before a specific date (inclusive)
            filters.append(Transaction.created_at <= datetime.fromisoformat(before_date))
        elif after_date:
            # After a specific date (inclusive)
            filters.append(Transaction.created_at >= datetime.fromisoformat(after_date))
    except ValueError:
        return jsonify({"error": "Invalid date format. Use ISO format (YYYY-MM-DD)."}), 400

    if filters:
        query = query.where(*filters)

    # --- Apply pagination and ordering
    query = query.order_by(Transaction.created_at.desc()).offset(offset).limit(limit)

    # --- Execute paginated query
    txns = db.execute(query).scalars().all()

    # --- Get total count (with same filters)
    total_count_query = select(func.count()).select_from(Transaction)
    if filters:
        total_count_query = total_count_query.where(*filters)
    total_count = db.execute(total_count_query).scalar()

    # --- Return paginated response
    return jsonify({
        "page": page,
        "limit": limit,
        "total": total_count,
        "results": txn_list_schema.dump(txns)
    }), 200


@transaction_bp.route("/transactions", methods=["POST"])
def create_transaction():
    db = g.db
    data = request.get_json() or {}

    # Validate that either product_id or child_product_id is provided (but not both)
    validation_error = validate_product_or_child_product_exclusive(data)
    if validation_error:
        return jsonify(validation_error[0]), validation_error[1]

    required_fields = ["quantity_delta", "reason"]
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"{field} is required"}), 400

    # Get the product or child_product and its quantity
    product = None
    child_product = None
    qty_record = None
    warehouse_id = g.active_warehouse_id
    
    if "product_id" in data and data["product_id"] is not None:
        product = db.get(Product, data["product_id"])
        if not product:
            return jsonify({"error": "Product or quantity record not found"}), 404
        qty_record = db.execute(
            select(Quantity).where(
                (Quantity.product_id == data["product_id"]) &
                (Quantity.warehouse_id == warehouse_id)
            )
        ).scalar_one_or_none()
        if not qty_record:
            return jsonify({"error": "Product or quantity record not found"}), 404
    else:
        child_product = db.get(ChildProduct, data["child_product_id"])
        if not child_product:
            return jsonify({"error": "Child product not found"}), 404
        qty_record = db.execute(
            select(Quantity).where(
                (Quantity.product_id == child_product.parent_product_id) &
                (Quantity.warehouse_id == warehouse_id)
            )
        ).scalar_one_or_none()
        if not qty_record:
            return jsonify({"error": "Parent product's quantity record not found"}), 404

    order = None
    order_item = None

    if data.get("order_id"):
        order = db.get(Order, data["order_id"])
        if not order:
            return jsonify({"error": "Order not found"}), 404

    if data.get("order_item_id"):
        order_item = db.get(OrderItem, data["order_item_id"])
        if not order_item:
            return jsonify({"error": "Order item not found"}), 404

    qty_delta = int(data["quantity_delta"])
    abs_qty = abs(qty_delta)

    # ===============================
    # Safety checks for order items
    # ===============================
    if order_item:
        remaining = order_item.quantity_ordered - order_item.quantity_fulfilled

        if abs_qty > remaining:
            return jsonify({
                "error": "Quantity exceeds remaining order item amount"
            }), 400

    # ===============================
    # Create PENDING transaction
    # ===============================
    txn = Transaction(
        product_id=product.id if product else None,
        child_product_id=child_product.id if child_product else None,
        order_id=order.id if order else None,
        order_item_id=order_item.id if order_item else None,
        warehouse_id=warehouse_id,
        quantity_delta=qty_delta,
        reason=data["reason"],
        note=data.get("note"),
        state=TransactionState.PENDING.value,
    )

    # ===============================
    # Apply PENDING inventory effect
    # ===============================
    if qty_delta < 0 :
        qty_record.reserved += abs_qty
    else :
       qty_record.ordered += abs_qty 


    db.add(txn)
    db.flush()

    #Auto_commit check

    auto_commit_flag = request.args.get("auto_commit") == "true"

    if auto_commit_flag:
        txn.commit(db)

    db.commit()

    return jsonify({
        "id": txn.id,
        "state": txn.state,
        "quantity_delta": txn.quantity_delta,
        "reason": txn.reason,
        "note": txn.note,
        "created_at": txn.created_at.isoformat(),
        "last_updated_at": txn.last_updated_at.isoformat(),
        "ledger_sequence": txn.ledger_sequence,
    }), 201


@transaction_bp.route("/transactions/produce", methods=["POST"])
def produce_product():
    """
    Atomically decrease inventory from a source product and increase another.
    """
    db = g.db
    data = request.get_json() or {}

    source_qty = data.get("source_quantity")
    target_qty = data.get("target_quantity")

    if source_qty is None or target_qty is None:
        return jsonify({"error": "source_quantity and target_quantity are required"}), 400

    try:
        source_qty = int(source_qty)
        target_qty = int(target_qty)
    except (TypeError, ValueError):
        return jsonify({"error": "Quantities must be integers"}), 400

    if source_qty <= 0 or target_qty <= 0:
        return jsonify({"error": "Quantities must be greater than zero"}), 400

    batch_id = data.get("batch_id")
    batch_payload = data.get("batch")

    if batch_id and batch_payload:
        return jsonify({"error": "Provide either batch_id or batch details, not both"}), 400

    source_entity, source_quantity, err, status = _get_entity_and_quantity(
        db,
        product_id=data.get("source_product_id"),
        child_product_id=data.get("source_child_product_id"),
        warehouse_id=g.active_warehouse_id,
    )
    if err:
        return jsonify(err), status

    target_entity, target_quantity, err, status = _get_entity_and_quantity(
        db,
        product_id=data.get("target_product_id"),
        child_product_id=data.get("target_child_product_id"),
        warehouse_id=g.active_warehouse_id,
    )
    if err:
        return jsonify(err), status

    if source_quantity.on_hand < source_qty:
        return jsonify({
            "error": "Not enough inventory to produce",
            "available": source_quantity.on_hand,
            "requested": source_qty,
        }), 409
    reason = data.get("reason", "adjustment")
    note = data.get("note")

    conversion_batch = None
    if batch_id:
        conversion_batch = db.get(ConversionBatch, batch_id)
        if not conversion_batch:
            return jsonify({"error": "Conversion batch not found"}), 404
    elif batch_payload:
        batch_order_id = batch_payload.get("order_id")
        if batch_order_id:
            order_for_batch = db.get(Order, batch_order_id)
            if not order_for_batch:
                return jsonify({"error": "Order not found for conversion batch"}), 404
        conversion_batch = ConversionBatch(
            order_id=batch_order_id,
            created_by=batch_payload.get("created_by"),
            note=batch_payload.get("note"),
            external_ref=batch_payload.get("external_ref"),
        )
        db.add(conversion_batch)

    try:
        timestamp = datetime.now(timezone.utc)

        consume_txn = Transaction(
            product_id=source_entity.id if isinstance(source_entity, Product) else None,
            child_product_id=source_entity.id if isinstance(source_entity, ChildProduct) else None,
            warehouse_id=g.active_warehouse_id,
            quantity_delta=-source_qty,
            reason=reason,
            note=note,
            state=TransactionState.COMMITTED.value,
            created_at=timestamp,
            last_updated_at=timestamp,
        )

        produce_txn = Transaction(
            product_id=target_entity.id if isinstance(target_entity, Product) else None,
            child_product_id=target_entity.id if isinstance(target_entity, ChildProduct) else None,
            warehouse_id=g.active_warehouse_id,
            quantity_delta=target_qty,
            reason=reason,
            note=note,
            state=TransactionState.COMMITTED.value,
            created_at=timestamp,
            last_updated_at=timestamp,
        )

        # Apply inventory changes atomically
        source_quantity.on_hand -= source_qty
        target_quantity.on_hand += target_qty

        db.add(consume_txn)
        db.add(produce_txn)
        db.flush()

        # Assign ledger sequences for directly-committed transactions
        consume_seq = db.execute(text("SELECT nextval('txn_ledger_seq')")).scalar()
        produce_seq = db.execute(text("SELECT nextval('txn_ledger_seq')")).scalar()
        consume_txn.ledger_sequence = consume_seq
        produce_txn.ledger_sequence = produce_seq

        conversion = Conversion(
            batch_id=conversion_batch.id if conversion_batch else None,
            increase_txn_id=produce_txn.id,
            state=ConversionState.COMPLETED.value,
            created_at=timestamp,
            note=data.get("conversion_note"),
        )
        conversion.decreases.append(ConversionDecrease(transaction_id=consume_txn.id))
        db.add(conversion)

        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({
            "error": "Failed to record conversion",
            "details": str(e)
        }), 400

    return jsonify({
        "message": "Production transaction completed",
        "consumed_transaction": txn_schema.dump(consume_txn),
        "produced_transaction": txn_schema.dump(produce_txn),
        "conversion": {
            "id": conversion.id,
            "batch_id": conversion.batch_id,
            "decreases": [
                {
                    "transaction_id": dec.transaction_id,
                    "product_id": dec.transaction.product_id,
                    "child_product_id": dec.transaction.child_product_id,
                }
                for dec in conversion.decreases
            ],
            "increase_txn_id": conversion.increase_txn_id,
            "state": conversion.state,
            "created_at": conversion.created_at.isoformat(),
            "note": conversion.note,
        },
        "source_on_hand": source_quantity.on_hand,
        "target_on_hand": target_quantity.on_hand,
    }), 201


@transaction_bp.route("/transactions/<int:txn_id>/commit", methods=["PATCH"])
def commit_transaction(txn_id):
    db = g.db
    txn = db.get(Transaction, txn_id)

    if not txn:
        return jsonify({"error": "Transaction not found"}), 404

    try:
        if txn.state != "pending":
            return jsonify({ "error": "Transaction is not pending"}), 400


        if txn.quantity_delta < 0:  # outgoing
            # Get quantity from the appropriate source
            qty_record = txn._get_quantity_record()
            qty = abs(txn.quantity_delta)

            if qty > qty_record.on_hand:
                return jsonify({ "error": f"Not enough inventory. On hand: {qty_record.on_hand}, required: {qty}"}), 409
   
        txn.commit(db)
        db.commit()
        return jsonify({
            "message": "Transaction committed successfully.",
            "transaction": txn_schema.dump(txn)
        }), 200
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400

@transaction_bp.route("/transactions/<int:txn_id>/cancel", methods=["PATCH"])
def cancel_transaction(txn_id):
    db = g.db
    txn = db.get(Transaction, txn_id)
    if not txn:
        return jsonify({"error": "Transaction not found"}), 404
    
    try:
        txn.cancel()
        db.commit()

        return jsonify({
            "message": "Transaction cancelled successfully.",
            "transaction": txn_schema.dump(txn)
        }), 200
    
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400



@transaction_bp.route("/transactions/<int:txn_id>/rollback", methods=["PATCH"])
def rollback_transaction(txn_id):
    db = g.db
    txn = db.get(Transaction, txn_id)

    if not txn:
        return jsonify({"error": "Transaction not found"}), 404

    try:
        new_txn = txn.rollback(db)
        db.commit()
        return jsonify({
            "message": "Transaction rolled back successfully.",
            "original_transaction": txn_schema.dump(txn),
            "rollback_transaction": txn_schema.dump(new_txn)
        }), 200

    except InventoryConflictError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 409

    except ValueError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400

    except Exception:
        db.rollback()
        return jsonify({"error": "Unexpected error while rolling back transaction"}), 500

# =====================================================
# 🔹 GET Pending Transactions with Order ETA (for projected stock graph)
# =====================================================
@transaction_bp.route("/transactions/pending_projection/<int:product_id>", methods=["GET"])
def get_pending_projection(product_id):
    """
    Return all pending transactions for a product and its child products,
    joined with their linked order's ETA. Used for the projected stock graph.
    """
    db = g.db

    warehouse_id = g.active_warehouse_id

    product = db.get(Product, product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404

    child_product_ids = db.execute(
        select(ChildProduct.id).where(ChildProduct.parent_product_id == product_id)
    ).scalars().all()

    product_filter = Transaction.product_id == product_id
    if child_product_ids:
        product_filter = or_(product_filter, Transaction.child_product_id.in_(child_product_ids))

    query = (
        select(
            Transaction.id,
            Transaction.quantity_delta,
            Transaction.order_id,
            Transaction.reason,
            Order.order_number,
            Order.type.label("order_type"),
            Order.eta,
        )
        .outerjoin(Order, Transaction.order_id == Order.id)
        .where(
            Transaction.state == TransactionState.PENDING.value,
            Transaction.warehouse_id == warehouse_id,
            product_filter,
        )
    )

    rows = db.execute(query).mappings().all()

    result = []
    for row in rows:
        row_dict = dict(row)
        if row_dict.get("eta") is not None:
            row_dict["eta"] = row_dict["eta"].strftime("%Y-%m-%d")
        result.append(row_dict)

    return jsonify(result), 200


# =====================================================
# 🔹 Helper for Ledger Queries
# =====================================================
def _get_transaction_ledger(db, product_id=None, child_product_id=None):
    """
    Helper function to retrieve transaction ledger for either a Product or ChildProduct.
    
    Args:
        db: Database session
        product_id: ID of the product (mutually exclusive with child_product_id)
        child_product_id: ID of the child product (mutually exclusive with product_id)
    
    Returns:
        dict: Ledger data with pagination and results
    """
    # --- Query parameters
    page = request.args.get("page", default=1, type=int)
    limit = request.args.get("limit", default=50, type=int)
    offset = (page - 1) * limit
    start_date = request.args.get("start_date", type=str)
    end_date = request.args.get("end_date", type=str)
    include_pending = request.args.get("include_pending", "false").lower() == "true"

    warehouse_id = g.active_warehouse_id

    # --- Build filters
    filters = [Transaction.warehouse_id == warehouse_id]
    
    if product_id:
        filters.append(Transaction.product_id == product_id)
        balance_filter_product_id = product_id
        balance_filter_child_product_id = None
    else:
        filters.append(Transaction.child_product_id == child_product_id)
        balance_filter_product_id = None
        balance_filter_child_product_id = child_product_id
    
    try:
        if start_date:
            filters.append(Transaction.created_at >= datetime.fromisoformat(start_date))
        if end_date:
            filters.append(Transaction.created_at <= datetime.fromisoformat(end_date))
    except ValueError:
        return {"error": "Invalid date format. Use ISO format (YYYY-MM-DD)."}, 400
    
    if not include_pending:
        filters.append(Transaction.state.in_(["committed", "rolled_back"]))

    # --- Define running balance (chronological)
    running_balance = func.sum(Transaction.quantity_delta).over(
        order_by=Transaction.created_at
    ).label("running_balance")

    # --- Main query (DESC output)
    query = (
        select(
            Transaction.id,
            Transaction.created_at,
            Transaction.last_updated_at,
            Transaction.reason,
            Transaction.quantity_delta,
            Transaction.order_id,
            Transaction.state,
            Transaction.note,
            Transaction.ledger_sequence,
            running_balance,
        )
        .where(*filters)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    # Execute paginated ledger
    results = db.execute(query).mappings().all()

    # Total count & final balance
    total_count = db.execute(
        select(func.count()).select_from(Transaction).where(*filters)
    ).scalar()

    # Build final balance query based on product type
    balance_query = select(func.sum(Transaction.quantity_delta)).where(
        Transaction.state.in_(["committed", "rolled_back"])
    )
    if balance_filter_product_id:
        balance_query = balance_query.where(Transaction.product_id == balance_filter_product_id)
    else:
        balance_query = balance_query.where(Transaction.child_product_id == balance_filter_child_product_id)
    
    final_balance = db.execute(balance_query).scalar() or 0

    result = {
        "page": page,
        "limit": limit,
        "total": total_count,
        "final_balance": final_balance,
        "results": [dict(row) for row in results],
    }
    
    if product_id:
        result["product_id"] = product_id
    else:
        result["child_product_id"] = child_product_id
    
    return result, 200


# =====================================================
# 🔹 GET Ledger for a Product
# =====================================================
@transaction_bp.route("/transactions/ledger/<int:product_id>", methods=["GET"])
def get_transaction_ledger(product_id):
    db = g.db

    # Ensure product exists
    product = db.get(Product, product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404

    result, status = _get_transaction_ledger(db, product_id=product_id)
    return jsonify(result), status


# =====================================================
# 🔹 GET Ledger for a Child Product
# =====================================================
@transaction_bp.route("/transactions/ledger/child_product/<int:child_product_id>", methods=["GET"])
def get_child_product_transaction_ledger(child_product_id):
    db = g.db

    # Ensure child product exists
    child_product = db.get(ChildProduct, child_product_id)
    if not child_product:
        return jsonify({"error": "Child product not found"}), 404

    result, status = _get_transaction_ledger(db, child_product_id=child_product_id)
    return jsonify(result), status
