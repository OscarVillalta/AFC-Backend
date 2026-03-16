from flask import g, jsonify, request, Blueprint
from sqlalchemy import select, text
from database.models import (
    Warehouse,
    Product,
    Quantity,
    Transaction,
    TransactionReason,
    TransactionState,
)
from datetime import datetime, timezone

transfer_bp = Blueprint("transfers", __name__)


@transfer_bp.route("/transfers", methods=["POST"])
def create_transfer():
    """
    Transfer stock of a product from one warehouse to another.

    Request body:
        product_id (int): ID of the product to transfer.
        from_warehouse_id (int): Source warehouse ID.
        to_warehouse_id (int): Destination warehouse ID.
        quantity (int): Number of units to transfer (must be > 0).

    The operation is atomic:
    1. Verify both warehouses and the product exist.
    2. Check the source warehouse has sufficient on_hand stock.
    3. Create a negative (outgoing) Transaction for the source warehouse.
    4. Create a positive (incoming) Transaction for the destination warehouse.
    5. Update the Quantity records for both warehouses.
    6. Commit everything in a single database transaction.
    """
    db = g.db
    data = request.get_json() or {}

    # ── Validate required fields ──────────────────────────────────────────────
    required = ["product_id", "from_warehouse_id", "to_warehouse_id", "quantity"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"'{field}' is required"}), 400

    try:
        product_id = int(data["product_id"])
        from_warehouse_id = int(data["from_warehouse_id"])
        to_warehouse_id = int(data["to_warehouse_id"])
        quantity = int(data["quantity"])
    except (ValueError, TypeError):
        return jsonify({"error": "product_id, from_warehouse_id, to_warehouse_id, and quantity must be integers"}), 400

    if quantity <= 0:
        return jsonify({"error": "quantity must be greater than zero"}), 400

    if from_warehouse_id == to_warehouse_id:
        return jsonify({"error": "from_warehouse_id and to_warehouse_id must be different"}), 400

    # ── Verify warehouses exist ───────────────────────────────────────────────
    from_warehouse = db.get(Warehouse, from_warehouse_id)
    if not from_warehouse:
        return jsonify({"error": f"Source warehouse {from_warehouse_id} not found"}), 404

    to_warehouse = db.get(Warehouse, to_warehouse_id)
    if not to_warehouse:
        return jsonify({"error": f"Destination warehouse {to_warehouse_id} not found"}), 404

    # ── Verify product exists ─────────────────────────────────────────────────
    product = db.get(Product, product_id)
    if not product:
        return jsonify({"error": f"Product {product_id} not found"}), 404

    # ── Fetch quantity records ────────────────────────────────────────────────
    from_qty = db.execute(
        select(Quantity).where(
            (Quantity.product_id == product_id) &
            (Quantity.warehouse_id == from_warehouse_id)
        )
    ).scalar_one_or_none()

    if not from_qty:
        return jsonify({
            "error": f"No quantity record found for product {product_id} in source warehouse {from_warehouse_id}"
        }), 404

    # ── Check sufficient stock ────────────────────────────────────────────────
    if from_qty.on_hand < quantity:
        return jsonify({
            "error": "Insufficient stock in source warehouse",
            "on_hand": from_qty.on_hand,
            "requested": quantity,
        }), 409

    # Fetch or create destination quantity record
    to_qty = db.execute(
        select(Quantity).where(
            (Quantity.product_id == product_id) &
            (Quantity.warehouse_id == to_warehouse_id)
        )
    ).scalar_one_or_none()

    if not to_qty:
        to_qty = Quantity(
            product_id=product_id,
            warehouse_id=to_warehouse_id,
            on_hand=0,
            reserved=0,
            ordered=0,
            location=0,
        )
        db.add(to_qty)
        db.flush()

    # ── Create ledger transactions & update quantities atomically ─────────────
    try:
        timestamp = datetime.now(timezone.utc)
        note = data.get("note") or f"Transfer of {quantity} units from warehouse {from_warehouse_id} to {to_warehouse_id}"

        # Outgoing transaction for source warehouse (negative delta)
        outgoing_txn = Transaction(
            product_id=product_id,
            warehouse_id=from_warehouse_id,
            quantity_delta=-quantity,
            reason=TransactionReason.TRANSFER.value,
            state=TransactionState.COMMITTED.value,
            note=note,
            created_at=timestamp,
            last_updated_at=timestamp,
        )

        # Incoming transaction for destination warehouse (positive delta)
        incoming_txn = Transaction(
            product_id=product_id,
            warehouse_id=to_warehouse_id,
            quantity_delta=quantity,
            reason=TransactionReason.TRANSFER.value,
            state=TransactionState.COMMITTED.value,
            note=note,
            created_at=timestamp,
            last_updated_at=timestamp,
        )

        # Update physical inventory
        from_qty.on_hand -= quantity
        to_qty.on_hand += quantity

        db.add(outgoing_txn)
        db.add(incoming_txn)
        db.flush()

        # Assign ledger sequences
        out_seq = db.execute(text("SELECT nextval('txn_ledger_seq')")).scalar()
        in_seq = db.execute(text("SELECT nextval('txn_ledger_seq')")).scalar()
        outgoing_txn.ledger_sequence = out_seq
        incoming_txn.ledger_sequence = in_seq

        db.commit()

    except Exception as exc:
        db.rollback()
        return jsonify({"error": "Transfer failed", "details": str(exc)}), 500

    return jsonify({
        "message": "Transfer completed successfully",
        "from_warehouse_id": from_warehouse_id,
        "to_warehouse_id": to_warehouse_id,
        "product_id": product_id,
        "quantity": quantity,
        "outgoing_transaction_id": outgoing_txn.id,
        "incoming_transaction_id": incoming_txn.id,
        "from_on_hand": from_qty.on_hand,
        "to_on_hand": to_qty.on_hand,
    }), 201
