from flask import Blueprint, g, jsonify
from sqlalchemy import select, func, desc
from flask_jwt_extended import jwt_required

from database.models import (
    Order,
    Transaction,
    ConversionBatch,
    Quantity,
)

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard/stats", methods=["GET"])
@jwt_required()
def get_dashboard_stats():
    db = g.db
    wh = g.active_warehouse_id

    # ── KPI counts ────────────────────────────────────────────────
    open_orders_count = db.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.status != "Completed")
        .where(Order.warehouse_id == wh)
    )

    pending_txns_count = db.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.state == "pending")
        .where(Transaction.warehouse_id == wh)
    )

    low_stock_count = db.scalar(
        select(func.count())
        .select_from(Quantity)
        .where(Quantity.available <= 0)
        .where(Quantity.warehouse_id == wh)
    )

    backordered_count = db.scalar(
        select(func.count())
        .select_from(Quantity)
        .where(Quantity.backordered > 0)
        .where(Quantity.warehouse_id == wh)
    )

    active_batches_count = db.scalar(
        select(func.count())
        .select_from(ConversionBatch)
        .where(ConversionBatch.warehouse_id == wh)
    )

    # ── Live feeds ────────────────────────────────────────────────
    recent_txns = db.execute(
        select(Transaction)
        .where(Transaction.state == "committed")
        .where(Transaction.warehouse_id == wh)
        .order_by(desc(Transaction.created_at))
        .limit(10)
    ).scalars().all()

    recent_transactions = [
        {
            "id": t.id,
            "quantity_delta": t.quantity_delta,
            "reason": t.reason,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in recent_txns
    ]

    recent_ords = db.execute(
        select(Order)
        .where(Order.status == "Completed")
        .where(Order.warehouse_id == wh)
        .order_by(desc(Order.completed_at))
        .limit(5)
    ).scalars().all()

    recent_orders = [
        {
            "id": o.id,
            "order_number": o.order_number,
            "type": o.type,
            "completed_at": o.completed_at.isoformat() if o.completed_at else None,
        }
        for o in recent_ords
    ]

    return jsonify({
        "kpis": {
            "open_orders": open_orders_count,
            "pending_txns": pending_txns_count,
            "low_stock": low_stock_count,
            "backordered": backordered_count,
            "active_batches": active_batches_count,
        },
        "feeds": {
            "recent_transactions": recent_transactions,
            "recent_orders": recent_orders,
        },
    })
