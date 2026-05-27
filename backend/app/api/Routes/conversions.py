from datetime import datetime, timezone
from flask import Blueprint, jsonify, request, g, current_app
from sqlalchemy import func, or_, select, text
from markupsafe import escape

from database.models import (
    Conversion,
    ConversionBatch,
    ConversionDecrease,
    ConversionState,
    Order,
    Product,
    ChildProduct,
    Quantity,
    Transaction,
    TransactionReason,
    TransactionState,
)
from app.api.validation import validate_pagination, ValidationError, sanitize_search_string



conversion_bp = Blueprint("conversions", __name__)


class InsufficientStockError(Exception):
    def __init__(self, product_id: int, on_hand: int, required: int):
        self.product_id = product_id
        self.on_hand = on_hand
        self.required = required
        super().__init__("Not enough inventory to convert.")


def _derive_state(conversion: Conversion) -> str:
    decrease_states = [dec.transaction.state for dec in conversion.decreases]
    increase_state = conversion.increase_txn.state

    if decrease_states and all(
        state == TransactionState.COMMITTED.value for state in decrease_states
    ) and increase_state == TransactionState.COMMITTED.value:
        return ConversionState.COMPLETED.value
    if decrease_states and all(
        state == TransactionState.ROLLED_BACK.value for state in decrease_states
    ) and increase_state == TransactionState.ROLLED_BACK.value:
        return ConversionState.ROLLED_BACK.value
    return "partial"


def _serialize_conversion(conversion: Conversion) -> dict:
    increase_txn = conversion.increase_txn

    return {
        "id": conversion.id,
        "batch_id": conversion.batch_id,
        "warehouse_id": conversion.warehouse_id,
        "note": conversion.note,
        "created_at": conversion.created_at.isoformat(),
        "state": _derive_state(conversion),
        "decreases": [
            {
                "product_id": dec_txn.transaction.product_id,
                "child_product_id": dec_txn.transaction.child_product_id,
                "quantity": abs(dec_txn.transaction.quantity_delta),
                "transaction_id": dec_txn.transaction.id,
            }
            for dec_txn in conversion.decreases
        ],
        "increase": {
            "product_id": increase_txn.product_id,
            "child_product_id": increase_txn.child_product_id,
            "quantity": abs(increase_txn.quantity_delta),
            "transaction_id": increase_txn.id,
        },
    }


def _serialize_batch(batch: ConversionBatch, conversions_total: int | None = None) -> dict:
    payload = {
        "id": batch.id,
        "order_id": batch.order_id,
        "warehouse_id": batch.warehouse_id,
        "note": batch.note,
        "created_by": batch.created_by,
        "created_at": batch.created_at.isoformat(),
        "external_ref": batch.external_ref,
    }
    if conversions_total is not None:
        payload["totals"] = {"conversions": conversions_total}
    return payload


def _resolve_order(db, order_id: int | None = None, external_ref: str | None = None) -> tuple[int | None, str | None, str | None, int | None]:
    """
    Resolve order_id and external_ref from the provided parameters.
    
    Args:
        db: Database session for querying orders
        order_id: Optional integer ID of the order
        external_ref: Optional string representing the external order number to look up
    
    Returns:
        Tuple of (resolved_order_id, resolved_external_ref, error_message, error_status_code)
        - resolved_order_id: The order ID to use (None if error or neither provided)
        - resolved_external_ref: The external reference to store (None if using order_id or error)
        - error_message: Error description if validation failed (None if successful)
        - error_status_code: HTTP status code for the error (None if successful)
    """
    if order_id is not None and external_ref is not None:
        return None, None, "Cannot provide both order_id and external_ref", 400
    
    if order_id is not None:
        order = db.get(Order, order_id)
        if not order:
            return None, None, f"Order with ID {order_id} not found", 404
        return order_id, None, None, None
    
    if external_ref is not None:
        order = db.execute(
            select(Order).where(Order.external_order_number == external_ref)
        ).scalar_one_or_none()
        if not order:
            return None, None, f"Order with external_order_number '{escape(external_ref)}' not found", 404
        return order.id, external_ref, None, None
    
    # Neither provided - this is valid (order_id can be None)
    return None, None, None, None


def _validate_and_get_product_with_quantity(db, product_id: int | None = None, child_product_id: int | None = None, warehouse_id: int | None = None):
    if product_id is not None:
        product = db.get(Product, product_id)
        if not product:
            raise ValueError("Product not found.")
        qty = db.execute(
            select(Quantity).where(
                (Quantity.product_id == product_id) &
                (Quantity.warehouse_id == warehouse_id)
            )
        ).scalar_one_or_none()
        if qty is None:
            raise ValueError("Quantity record not found for product in this warehouse.")
        return product, qty

    if child_product_id is not None:
        child = db.get(ChildProduct, child_product_id)
        if not child:
            raise ValueError("Child product not found.")
        qty = db.execute(
            select(Quantity).where(
                (Quantity.product_id == child.parent_product_id) &
                (Quantity.warehouse_id == warehouse_id)
            )
        ).scalar_one_or_none()
        if qty is None:
            raise ValueError("Quantity record not found for child product in this warehouse.")
        return child, qty

    raise ValueError("product_id or child_product_id is required.")


def _get_quantity_record_from_transaction(txn: Transaction):
    """Delegate to Transaction._get_quantity_record() which is warehouse-aware."""
    return txn._get_quantity_record()


def _validate_conversion_payload(payload: dict):
    if not isinstance(payload, dict):
        raise ValueError("Conversion payload is required.")

    decrease_payload = payload.get("decreases") or payload.get("decrease")
    if not decrease_payload:
        raise ValueError("At least one decrease entry is required.")

    if isinstance(decrease_payload, dict):
        decrease_entries = [decrease_payload]
    elif isinstance(decrease_payload, list):
        decrease_entries = decrease_payload
    else:
        raise ValueError("Invalid decreases payload.")

    increase = payload.get("increase") or {}

    validated_decreases = []
    for dec in decrease_entries:
        product_id = dec.get("product_id")
        child_product_id = dec.get("child_product_id")

        if product_id is None and child_product_id is None:
            raise ValueError("product_id or child_product_id is required for decreases.")
        if product_id is not None and child_product_id is not None:
            raise ValueError("Provide either product_id or child_product_id, not both.")

        try:
            qty = int(dec.get("quantity"))
            product_id = int(product_id) if product_id is not None else None
            child_product_id = int(child_product_id) if child_product_id is not None else None
        except (TypeError, ValueError):
            raise ValueError("product_id/child_product_id and quantity must be integers.")
        if qty <= 0:
            raise ValueError("Quantities must be greater than zero.")
        validated_decreases.append(
            {"product_id": product_id, "child_product_id": child_product_id, "quantity": qty}
        )

    increase_product_id = increase.get("product_id")
    increase_child_product_id = increase.get("child_product_id")
    if increase_product_id is None and increase_child_product_id is None:
        raise ValueError("increase.product_id or increase.child_product_id is required.")
    if increase_product_id is not None and increase_child_product_id is not None:
        raise ValueError("Provide either product_id or child_product_id for increase, not both.")

    try:
        increase_product_id = int(increase_product_id) if increase_product_id is not None else None
        increase_child_product_id = (
            int(increase_child_product_id) if increase_child_product_id is not None else None
        )
        increase_qty = int(increase.get("quantity"))
    except (TypeError, ValueError):
        raise ValueError("product_id/child_product_id and quantity must be integers.")

    if increase_qty <= 0:
        raise ValueError("Quantities must be greater than zero.")

    for dec in validated_decreases:
        if dec["product_id"] and dec["product_id"] == increase_product_id:
            raise ValueError("Decrease and increase products must be different.")
        if dec["child_product_id"] and dec["child_product_id"] == increase_child_product_id:
            raise ValueError("Decrease and increase products must be different.")

    return validated_decreases, increase_product_id, increase_child_product_id, increase_qty


def _create_conversion(db, batch: ConversionBatch, payload: dict, warehouse_id: int) -> Conversion:
    decreases, increase_product_id, increase_child_product_id, increase_qty = _validate_conversion_payload(payload)

    decrease_products = []
    for dec in decreases:
        product, quantity = _validate_and_get_product_with_quantity(
            db, dec.get("product_id"), dec.get("child_product_id"), warehouse_id=warehouse_id
        )
        if quantity.on_hand < dec["quantity"]:
            raise InsufficientStockError(
                product_id=dec.get("product_id") or dec.get("child_product_id"),
                on_hand=quantity.on_hand,
                required=dec["quantity"],
            )
        decrease_products.append((product, quantity, dec["quantity"]))

    increase_product, increase_quantity = _validate_and_get_product_with_quantity(
        db, increase_product_id, increase_child_product_id, warehouse_id=warehouse_id
    )

    timestamp = datetime.now(timezone.utc)
    note = payload.get("note")

    decrease_txns: list[Transaction] = []
    for product, quantity, decrease_qty in decrease_products:
        consume_txn = Transaction(
            product_id=product.id if isinstance(product, Product) else None,
            child_product_id=product.id if isinstance(product, ChildProduct) else None,
            warehouse_id=warehouse_id,
            quantity_delta=-decrease_qty,
            reason=TransactionReason.ADJUSTMENT.value,
            note=note,
            state=TransactionState.COMMITTED.value,
            created_at=timestamp,
            last_updated_at=timestamp,
            order_id=batch.order_id,
        )
        quantity.on_hand -= decrease_qty
        db.add(consume_txn)
        decrease_txns.append(consume_txn)

    produce_txn = Transaction(
        product_id=increase_product.id if isinstance(increase_product, Product) else None,
        child_product_id=increase_product.id if isinstance(increase_product, ChildProduct) else None,
        warehouse_id=warehouse_id,
        quantity_delta=increase_qty,
        reason=TransactionReason.ADJUSTMENT.value,
        note=note,
        state=TransactionState.COMMITTED.value,
        created_at=timestamp,
        last_updated_at=timestamp,
        order_id=batch.order_id,
    )

    increase_quantity.on_hand += increase_qty

    db.add(produce_txn)
    db.flush()

    # Assign ledger sequences for directly-committed transactions
    for txn in decrease_txns:
        seq_val = db.execute(text("SELECT nextval('txn_ledger_seq')")).scalar()
        txn.ledger_sequence = seq_val
    produce_seq = db.execute(text("SELECT nextval('txn_ledger_seq')")).scalar()
    produce_txn.ledger_sequence = produce_seq

    conversion = Conversion(
        batch_id=batch.id,
        warehouse_id=warehouse_id,
        increase_txn_id=produce_txn.id,
        created_at=timestamp,
        state=ConversionState.COMPLETED.value,
        note=note,
    )
    for txn in decrease_txns:
        conversion.decreases.append(ConversionDecrease(transaction_id=txn.id))
    batch.conversions.append(conversion)
    db.add(conversion)
    db.flush()
    return conversion


@conversion_bp.route("/conversion_batches", methods=["POST"])
def create_conversion_batch():
    db = g.db
    data = request.get_json() or {}

    conversions_payload = data.get("conversions") or []
    if not conversions_payload:
        return jsonify({"error": "At least one conversion is required."}), 400

    # Resolve order_id and external_ref
    order_id_input = data.get("order_id")
    external_ref_input = data.get("external_ref")
    resolved_order_id, resolved_external_ref, error, error_code = _resolve_order(db, order_id_input, external_ref_input)
    if error:
        return jsonify({"error": error}), error_code

    batch = ConversionBatch(
        order_id=resolved_order_id,
        warehouse_id=g.active_warehouse_id,
        note=data.get("note"),
        created_by=data.get("created_by"),
        external_ref=resolved_external_ref,
    )
    db.add(batch)
    db.flush()

    try:
        conversions = [_create_conversion(db, batch, payload, warehouse_id=g.active_warehouse_id) for payload in conversions_payload]
        db.commit()
    except InsufficientStockError as e:
        db.rollback()
        return (
            jsonify(
                {
                    "error": "Not enough inventory to convert.",
                    "details": {
                        "product_id": e.product_id,
                        "on_hand": e.on_hand,
                        "required": e.required,
                    },
                }
            ),
            409,
        )
    except ValueError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        db.rollback()
        current_app.logger.exception("Error creating conversion batch")
        return jsonify({"error": "Failed to create conversion batch. See server logs for details."}), 500

    return (
        jsonify(
            {
                "batch": _serialize_batch(batch),
                "conversions": [_serialize_conversion(conv) for conv in conversions],
            }
        ),
        201,
    )


@conversion_bp.route("/conversion_batches/search", methods=["GET"])
def search_conversion_batches():
    db = g.db
    try:
        page, limit = validate_pagination(
            request.args.get("page"), request.args.get("limit"), default_limit=25
        )
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400

    offset = (page - 1) * limit

    filters = []
    order_id = request.args.get("order_id", type=int)
    created_by = request.args.get("created_by")
    query_str = request.args.get("q")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "all")

    # Scope to the active warehouse
    filters.append(ConversionBatch.warehouse_id == g.active_warehouse_id)

    if order_id:
        filters.append(ConversionBatch.order_id == order_id)
    if created_by:
        filters.append(ConversionBatch.created_by == created_by)
    if query_str:
        safe_query = sanitize_search_string(query_str)
        filters.append(ConversionBatch.note.ilike(f"%{safe_query}%"))

    # Status-based filters
    if status == "has_order":
        filters.append(ConversionBatch.order_id.isnot(None))
    elif status == "reversed":
        filters.append(
            ConversionBatch.conversions.any(
                Conversion.state == ConversionState.ROLLED_BACK.value
            )
        )

    # Search by batch ID or order ID
    if search:
        try:
            parsed_int = int(search)
            filters.append(
                or_(
                    ConversionBatch.id == parsed_int,
                    ConversionBatch.order_id == parsed_int,
                )
            )
        except ValueError:
            pass

    if date_from:
        try:
            parsed_from = datetime.fromisoformat(date_from)
            if parsed_from.tzinfo is None:
                parsed_from = parsed_from.replace(tzinfo=timezone.utc)
            filters.append(ConversionBatch.created_at >= parsed_from)
        except ValueError:
            return jsonify({"error": "Invalid date_from format."}), 400
    if date_to:
        try:
            parsed_to = datetime.fromisoformat(date_to)
            if parsed_to.tzinfo is None:
                parsed_to = parsed_to.replace(tzinfo=timezone.utc)
            filters.append(ConversionBatch.created_at <= parsed_to)
        except ValueError:
            return jsonify({"error": "Invalid date_to format."}), 400

    counts_subquery = (
        select(Conversion.batch_id, func.count(Conversion.id).label("conv_count"))
        .group_by(Conversion.batch_id)
        .subquery()
    )

    query = (
        select(ConversionBatch, counts_subquery.c.conv_count)
        .join(counts_subquery, ConversionBatch.id == counts_subquery.c.batch_id, isouter=True)
        .where(*filters)
        .order_by(ConversionBatch.created_at.desc())
        .offset(offset)
        .limit(limit)
    )

    results = db.execute(query).all()
    total = db.execute(
        select(func.count()).select_from(ConversionBatch).where(*filters)
    ).scalar()

    return (
        jsonify(
            {
                "page": page,
                "limit": limit,
                "total": total,
                "results": [
                    _serialize_batch(batch, conversions_total=count or 0) for batch, count in results
                ],
            }
        ),
        200,
    )


@conversion_bp.route("/conversion_batches/<int:batch_id>", methods=["GET"])
def get_conversion_batch(batch_id: int):
    db = g.db
    batch = db.get(ConversionBatch, batch_id)
    if not batch:
        return jsonify({"error": "Conversion batch not found"}), 404

    conversions = (
        db.execute(
            select(Conversion)
            .where(Conversion.batch_id == batch_id)
            .order_by(Conversion.created_at.asc())
        )
        .scalars()
        .all()
    )

    return (
        jsonify(
            {
                "batch": _serialize_batch(batch),
                "conversions": [_serialize_conversion(conv) for conv in conversions],
            }
        ),
        200,
    )


@conversion_bp.route("/conversion_batches/<int:batch_id>/conversions", methods=["POST"])
def add_conversion_to_batch(batch_id: int):
    db = g.db
    batch = db.get(ConversionBatch, batch_id)
    if not batch:
        return jsonify({"error": "Conversion batch not found"}), 404

    if batch.warehouse_id != g.active_warehouse_id:
        return jsonify({"error": "Conversion batch belongs to a different warehouse."}), 403

    payload = request.get_json() or {}
    try:
        conversion = _create_conversion(db, batch, payload, warehouse_id=batch.warehouse_id)
        db.commit()
    except InsufficientStockError as e:
        db.rollback()
        return (
            jsonify(
                {
                    "error": "Not enough inventory to convert.",
                    "details": {
                        "product_id": e.product_id,
                        "on_hand": e.on_hand,
                        "required": e.required,
                    },
                }
            ),
            409,
        )
    except ValueError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        db.rollback()
        current_app.logger.exception("Error adding conversion to batch %s", batch_id)
        return jsonify({"error": "Failed to add conversion. See server logs for details."}), 500

    return jsonify({"conversion": _serialize_conversion(conversion)}), 201


@conversion_bp.route("/conversion_batches/<int:batch_id>", methods=["PATCH"])
def update_conversion_batch(batch_id: int):
    db = g.db
    data = request.get_json() or {}

    batch = db.get(ConversionBatch, batch_id)
    if not batch:
        return jsonify({"error": "Conversion batch not found"}), 404

    # Handle order_id and external_ref updates
    if "order_id" in data or "external_ref" in data:
        order_id_input = data.get("order_id")
        external_ref_input = data.get("external_ref")
        
        resolved_order_id, resolved_external_ref, error, error_code = _resolve_order(db, order_id_input, external_ref_input)
        if error:
            return jsonify({"error": error}), error_code
        
        batch.order_id = resolved_order_id
        batch.external_ref = resolved_external_ref

    if "note" in data:
        batch.note = data.get("note")

    db.commit()
    return jsonify({"batch": _serialize_batch(batch)}), 200


@conversion_bp.route("/conversions/<int:conversion_id>/rollback", methods=["PATCH"])
def rollback_conversion(conversion_id: int):
    db = g.db
    payload = request.get_json() or {}
    conversion = db.get(Conversion, conversion_id)

    if not conversion:
        return jsonify({"error": "Conversion not found"}), 404

    state = _derive_state(conversion)
    if state == "rolled_back":
        return jsonify({"error": "Conversion already rolled back"}), 400

    increase_txn = conversion.increase_txn
    qty_record = _get_quantity_record_from_transaction(increase_txn)
    required = abs(increase_txn.quantity_delta)

    if qty_record and qty_record.on_hand < required:
        return (
            jsonify(
                {
                    "error": "Cannot roll back conversion due to insufficient on_hand.",
                    "details": {
                        "product_id": increase_txn.product_id,
                        "on_hand": qty_record.on_hand,
                        "required": required,
                    },
                }
            ),
            409,
        )

    try:
        decrease_rbs = [dec.transaction.rollback(db) for dec in conversion.decreases]
        increase_rb = conversion.increase_txn.rollback(db)

        if payload.get("note"):
            for rb in decrease_rbs:
                rb.note = payload["note"]
            increase_rb.note = payload["note"]

        conversion.state = ConversionState.ROLLED_BACK.value
        db.commit()
    except ValueError as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception:
        db.rollback()
        current_app.logger.exception("Error rolling back conversion %s", conversion_id)
        return jsonify({"error": "Failed to roll back conversion. See server logs for details."}), 500

    return (
        jsonify(
            {
                "message": "Conversion rolled back.",
                "conversion": {
                    "id": conversion.id,
                    "batch_id": conversion.batch_id,
                    "state": "rolled_back",
                        "rollback": {
                            "decrease_rollback_transaction_ids": [rb.id for rb in decrease_rbs],
                            "increase_rollback_transaction_id": increase_rb.id,
                        },
                },
            }
        ),
        200,
    )


@conversion_bp.route("/conversion_batches/<int:batch_id>/rollback", methods=["PATCH"])
def rollback_conversion_batch(batch_id: int):
    db = g.db
    batch = db.get(ConversionBatch, batch_id)
    if not batch:
        return jsonify({"error": "Conversion batch not found"}), 404

    conversions = (
        db.execute(select(Conversion).where(Conversion.batch_id == batch_id)).scalars().all()
    )

    to_rollback = []
    skipped = []
    for conv in conversions:
        state = _derive_state(conv)
        if state == "rolled_back":
            skipped.append(conv.id)
            continue

        increase_txn = conv.increase_txn
        qty_record = _get_quantity_record_from_transaction(increase_txn)
        required = abs(increase_txn.quantity_delta)
        if qty_record and qty_record.on_hand < required:
            return (
                jsonify(
                    {
                        "error": "Cannot roll back conversion due to insufficient on_hand.",
                        "details": {
                            "product_id": increase_txn.product_id,
                            "on_hand": qty_record.on_hand,
                            "required": required,
                        },
                    }
                ),
                409,
            )

        to_rollback.append(conv)

    rolled_back_ids = []
    try:
        for conv in to_rollback:
            for dec in conv.decreases:
                dec.transaction.rollback(db)
            conv.increase_txn.rollback(db)
            conv.state = ConversionState.ROLLED_BACK.value
            rolled_back_ids.append(conv.id)
        db.commit()
    except Exception:
        db.rollback()
        current_app.logger.exception("Error rolling back conversion batch %s", batch_id)
        return jsonify({"error": "Failed to roll back conversion batch. See server logs for details."}), 500

    return (
        jsonify(
            {
                "message": "Batch rolled back.",
                "batch_id": batch_id,
                "results": {"rolled_back": rolled_back_ids, "skipped": skipped},
            }
        ),
        200,
    )


@conversion_bp.route("/conversions/search", methods=["GET"])
def search_conversions():
    db = g.db
    try:
        page, limit = validate_pagination(
            request.args.get("page"), request.args.get("limit"), default_limit=25
        )
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400

    offset = (page - 1) * limit

    filters = []
    batch_id = request.args.get("batch_id", type=int)
    order_id = request.args.get("order_id", type=int)
    product_id = request.args.get("product_id", type=int)

    # Scope to the active warehouse
    filters.append(Conversion.warehouse_id == g.active_warehouse_id)

    if batch_id:
        filters.append(Conversion.batch_id == batch_id)

    if order_id:
        filters.append(Conversion.batch.has(ConversionBatch.order_id == order_id))

    if product_id:
        filters.append(
            or_(
                Conversion.decreases.any(
                    ConversionDecrease.transaction.has(
                        or_(
                            Transaction.product_id == product_id,
                            Transaction.child_product_id == product_id,
                        )
                    )
                ),
                Conversion.increase_txn.has(
                    or_(
                        Transaction.product_id == product_id,
                        Transaction.child_product_id == product_id,
                    )
                ),
            )
        )

    conversions = (
        db.execute(
            select(Conversion)
            .where(*filters)
            .order_by(Conversion.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        .scalars()
        .all()
    )

    total = db.execute(select(func.count()).select_from(Conversion).where(*filters)).scalar()

    return (
        jsonify(
            {
                "page": page,
                "limit": limit,
                "total": total,
                "results": [
                    {**_serialize_conversion(conv), "order_id": conv.batch.order_id if conv.batch else None}
                    for conv in conversions
                ],
            }
        ),
        200,
    )
