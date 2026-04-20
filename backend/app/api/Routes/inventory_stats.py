from flask import g, jsonify, request, Blueprint
from sqlalchemy import func, select, case, desc, or_
from sqlalchemy.orm import selectinload
from database.models import Product, Quantity, AirFilter, StockItem, Media, ProductCategory


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


@inventory_stats_bp.route("/inventory/top-items", methods=["GET"])
def get_top_items():
    """
    Return the top 20 items by the requested field (on_hand, available, backordered, reserved, or ordered)
    along with a sum of "All Others" to support pie chart rendering.
    All queries are scoped to the active warehouse.
    """
    db = g.db
    warehouse_id = g.active_warehouse_id
    
    # Get query parameters
    field = request.args.get("field", default="on_hand", type=str)
    limit = request.args.get("limit", default=20, type=int)
    
    # Validate field parameter
    valid_fields = ["on_hand", "available", "backordered", "reserved", "ordered"]
    if field not in valid_fields:
        return jsonify({"error": f"Invalid field. Must be one of: {', '.join(valid_fields)}"}), 400
    
    # Validate limit
    if limit < 1 or limit > 100:
        return jsonify({"error": "Limit must be between 1 and 100"}), 400
    
    # Build the field expression based on the requested field
    if field == "available":
        # available = max(on_hand - reserved, 0)
        field_expr = func.greatest(Quantity.on_hand - Quantity.reserved, 0)
    elif field == "backordered":
        # backordered = abs(min(0, on_hand - reserved))
        field_expr = func.abs(func.least(0, Quantity.on_hand - Quantity.reserved))
    else:
        # For on_hand, reserved, ordered - direct column access
        field_expr = getattr(Quantity, field)
    
    # Get top N product IDs and their field values
    top_items_subquery = (
        select(
            Quantity.product_id,
            Quantity.on_hand,
            Quantity.reserved,
            Quantity.ordered,
            func.greatest(Quantity.on_hand - Quantity.reserved, 0).label("available"),
            func.abs(func.least(0, Quantity.on_hand - Quantity.reserved)).label("backordered"),
            field_expr.label("sort_field")
        )
        .where(Quantity.warehouse_id == warehouse_id)
        .order_by(desc("sort_field"))
        .limit(limit)
    ).subquery()
    
    # Fetch full products with relationships for the top items
    top_products = db.execute(
        select(Product)
        .join(top_items_subquery, Product.id == top_items_subquery.c.product_id)
        .options(
            selectinload(Product.air_filter),
            selectinload(Product.stock_item),
            selectinload(Product.media)
        )
    ).scalars().all()
    
    # Create a lookup for quantity values by product_id
    qty_lookup = {}
    top_ids = []
    for row in db.execute(select(top_items_subquery)).all():
        qty_lookup[row.product_id] = {
            "on_hand": row.on_hand,
            "reserved": row.reserved,
            "ordered": row.ordered,
            "available": row.available,
            "backordered": row.backordered,
            "sort_field": row.sort_field
        }
        top_ids.append(row.product_id)
    
    # Build top items response
    top_items = []
    for product in top_products:
        qty_data = qty_lookup.get(product.id, {})
        
        # Determine product name based on type
        if product.air_filter:
            product_name = product.air_filter.part_number
        elif product.stock_item:
            product_name = product.stock_item.name
        elif product.media:
            product_name = product.media.part_number
        else:
            product_name = f"Product {product.id}"
        
        top_items.append({
            "product_id": product.id,
            "product_name": product_name,
            "on_hand": qty_data.get("on_hand", 0),
            "available": qty_data.get("available", 0),
            "reserved": qty_data.get("reserved", 0),
            "ordered": qty_data.get("ordered", 0),
            "backordered": qty_data.get("backordered", 0)
        })
    
    # Calculate "All Others" sum
    # Get sum of the field for all items not in top N
    all_others_sum = 0
    if top_ids:
        all_others_query = (
            select(func.coalesce(func.sum(field_expr), 0))
            .select_from(Quantity)
            .where(
                Quantity.warehouse_id == warehouse_id,
                Quantity.product_id.notin_(top_ids)
            )
        )
        all_others_sum = db.scalar(all_others_query) or 0
    else:
        # If no top items, sum all
        all_query = (
            select(func.coalesce(func.sum(field_expr), 0))
            .select_from(Quantity)
            .where(Quantity.warehouse_id == warehouse_id)
        )
        all_others_sum = db.scalar(all_query) or 0
    
    # Calculate total for percentage calculations
    total_query = (
        select(func.coalesce(func.sum(field_expr), 0))
        .select_from(Quantity)
        .where(Quantity.warehouse_id == warehouse_id)
    )
    total_sum = db.scalar(total_query) or 0
    
    return jsonify({
        "field": field,
        "limit": limit,
        "total": int(total_sum),
        "top_items": top_items,
        "all_others": int(all_others_sum)
    }), 200
