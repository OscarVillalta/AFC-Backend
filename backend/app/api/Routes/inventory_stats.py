from flask import g, jsonify, Blueprint
from sqlalchemy import func, select, case
from database.models import Product, Quantity


inventory_stats_bp = Blueprint("inventory_stats", __name__)


@inventory_stats_bp.route("/inventory/stats", methods=["GET"])
def get_inventory_stats():
    db = g.db
    warehouse_id = g.active_warehouse_id

    # Only count parent products that have a Quantity row in the active warehouse
    base = (
        select(
            func.count(Quantity.id).label("total_skus"),
            func.coalesce(func.sum(Quantity.reserved), 0).label("reserved_total"),
            func.coalesce(func.sum(Quantity.ordered), 0).label("ordered_total"),
            func.coalesce(
                func.sum(case((Quantity.on_hand <= 0, 1), else_=0)), 0
            ).label("low_stock_skus"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            (Quantity.reserved > 0) & (Quantity.on_hand < Quantity.reserved),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("backordered_skus"),
        )
        .select_from(Quantity)
        .join(Product, Product.id == Quantity.product_id)
        .where(Quantity.warehouse_id == warehouse_id)
    )

    row = db.execute(base).mappings().one()

    return jsonify({
        "total_skus": row["total_skus"],
        "low_stock_skus": row["low_stock_skus"],
        "backordered_skus": row["backordered_skus"],
        "reserved_total": int(row["reserved_total"]),
        "ordered_total": int(row["ordered_total"]),
    }), 200
