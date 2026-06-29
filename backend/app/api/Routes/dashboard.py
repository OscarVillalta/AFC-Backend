from flask import Blueprint, g, jsonify, request
from sqlalchemy import select, func, desc, and_, or_
from sqlalchemy.orm import selectinload
from flask_jwt_extended import jwt_required
from datetime import datetime, timedelta, timezone
from typing import Optional

from database.models import (
    Order,
    Transaction,
    ConversionBatch,
    Quantity,
    TransactionReason,
    Product,
    ChildProduct,
)

dashboard_bp = Blueprint("dashboard", __name__)


def _product_display_name(product: Product) -> str:
    if product.air_filter:
        return product.air_filter.part_number
    if product.stock_item:
        return product.stock_item.name
    if product.media:
        return product.media.part_number
    return f"Product {product.id}"


def _child_product_display_name(child: ChildProduct) -> str:
    if child.air_filter:
        return child.air_filter.part_number
    if child.stock_item:
        return child.stock_item.name
    if child.media:
        return child.media.part_number
    return f"Child {child.id}"


def _transaction_feed_entry(t: Transaction) -> dict:
    product_id = t.product_id
    product_name = None

    if t.child_product:
        product_id = t.child_product.parent_product_id
        product_name = _child_product_display_name(t.child_product)
    elif t.product:
        product_id = t.product.id
        product_name = _product_display_name(t.product)

    order_number = None
    external_order_number = None
    if t.order:
        order_number = t.order.order_number
        external_order_number = t.order.external_order_number

    return {
        "id": t.id,
        "quantity_delta": t.quantity_delta,
        "reason": t.reason,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "order_id": t.order_id,
        "order_number": order_number,
        "external_order_number": external_order_number,
        "product_id": product_id,
        "product_name": product_name,
    }


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
        .options(
            selectinload(Transaction.order),
            selectinload(Transaction.product).selectinload(Product.air_filter),
            selectinload(Transaction.product).selectinload(Product.stock_item),
            selectinload(Transaction.product).selectinload(Product.media),
            selectinload(Transaction.child_product).selectinload(ChildProduct.air_filter),
            selectinload(Transaction.child_product).selectinload(ChildProduct.stock_item),
            selectinload(Transaction.child_product).selectinload(ChildProduct.media),
        )
        .order_by(desc(Transaction.created_at))
        .limit(10)
    ).scalars().all()

    recent_transactions = [_transaction_feed_entry(t) for t in recent_txns]

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
            "external_order_number": o.external_order_number,
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


@dashboard_bp.route("/dashboard/net-kpis", methods=["GET"])
@jwt_required()
def get_net_kpis():
    """
    Calculate high-performance, aggregated KPI data for the Enterprise Operations Dashboard.
    Returns net delivered, received, reserved, ordered, and backordered quantities.
    """
    db = g.db
    wh = g.active_warehouse_id
    
    # Get days parameter, default to 30
    days = request.args.get("days", default=30, type=int)
    
    # Validate days parameter
    if days < 1 or days > 365:
        return jsonify({"error": "days parameter must be between 1 and 365"}), 400
    
    # Calculate the date threshold
    date_threshold = datetime.now(timezone.utc) - timedelta(days=days)
    prev_date_threshold = date_threshold - timedelta(days=days)
    
    def calc_pct(current: int, previous: int) -> Optional[float]:
        if previous == 0:
            return 100.0 if current > 0 else 0.0
        return round(((current - previous) / abs(previous)) * 100, 1)
    
    # Net Delivered: sum of quantity_delta for fulfillment and sale reasons in the last N days
    # Note: TransactionReason enum uses 'shipment' not 'fulfillment', and there's no 'sale' in the enum
    # Based on the enum, we'll use 'shipment' for outgoing deliveries
    net_delivered = db.scalar(
        select(func.coalesce(func.sum(Transaction.quantity_delta), 0))
        .where(
            Transaction.warehouse_id == wh,
            Transaction.state == "committed",
            Transaction.created_at >= date_threshold,
            Transaction.reason == TransactionReason.SHIPMENT.value
        )
    ) or 0
    
    net_delivered_prev = db.scalar(
        select(func.coalesce(func.sum(Transaction.quantity_delta), 0))
        .where(
            Transaction.warehouse_id == wh,
            Transaction.state == "committed",
            Transaction.created_at >= prev_date_threshold,
            Transaction.created_at < date_threshold,
            Transaction.reason == TransactionReason.SHIPMENT.value
        )
    ) or 0
    
    # Net Received: sum of quantity_delta for receive reason and positive adjustments in the last N days
    net_received = db.scalar(
        select(func.coalesce(func.sum(Transaction.quantity_delta), 0))
        .where(
            Transaction.warehouse_id == wh,
            Transaction.state == "committed",
            Transaction.created_at >= date_threshold,
            or_(
                Transaction.reason == TransactionReason.RECEIVE.value,
                and_(
                    Transaction.reason == TransactionReason.ADJUSTMENT.value,
                    Transaction.quantity_delta > 0
                )
            )
        )
    ) or 0
    
    net_received_prev = db.scalar(
        select(func.coalesce(func.sum(Transaction.quantity_delta), 0))
        .where(
            Transaction.warehouse_id == wh,
            Transaction.state == "committed",
            Transaction.created_at >= prev_date_threshold,
            Transaction.created_at < date_threshold,
            or_(
                Transaction.reason == TransactionReason.RECEIVE.value,
                and_(
                    Transaction.reason == TransactionReason.ADJUSTMENT.value,
                    Transaction.quantity_delta > 0
                )
            )
        )
    ) or 0
    
    # Net Reserved: Current sum of Quantity.reserved
    net_reserved = db.scalar(
        select(func.coalesce(func.sum(Quantity.reserved), 0))
        .where(Quantity.warehouse_id == wh)
    ) or 0
    
    # Net Ordered: Current sum of Quantity.ordered
    net_ordered = db.scalar(
        select(func.coalesce(func.sum(Quantity.ordered), 0))
        .where(Quantity.warehouse_id == wh)
    ) or 0
    
    # Net Backordered: Current sum of Quantity.backordered
    # backordered is a hybrid property calculated as abs(min(0, on_hand - reserved))
    # We need to calculate this in SQL
    net_backordered = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    func.abs(func.least(0, Quantity.on_hand - Quantity.reserved))
                ), 
                0
            )
        )
        .where(Quantity.warehouse_id == wh)
    ) or 0
    
    return jsonify({
        "net_delivered": int(net_delivered),
        "net_received": int(net_received),
        "net_reserved": int(net_reserved),
        "net_ordered": int(net_ordered),
        "net_backordered": int(net_backordered),
        "net_delivered_pct": calc_pct(int(net_delivered), int(net_delivered_prev)),
        "net_received_pct": calc_pct(int(net_received), int(net_received_prev)),
        "days": days,
    })
