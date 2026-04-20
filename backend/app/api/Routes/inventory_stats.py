from flask import g, jsonify, request, Blueprint
from sqlalchemy import func, select, case, desc, or_
from database.models import Product, Quantity, AirFilter, StockItem, ProductCategory


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
    
    # Build base query with joins to get product details
    base_query = (
        select(
            Product.id.label("product_id"),
            Product.category_id,
            Product.reference_id,
            Quantity.on_hand,
            Quantity.reserved,
            Quantity.ordered,
            func.greatest(Quantity.on_hand - Quantity.reserved, 0).label("available"),
            func.abs(func.least(0, Quantity.on_hand - Quantity.reserved)).label("backordered"),
            field_expr.label("sort_field")
        )
        .select_from(Quantity)
        .join(Product, Product.id == Quantity.product_id)
        .where(Quantity.warehouse_id == warehouse_id)
        .order_by(desc("sort_field"))
    )
    
    # Get top N items
    top_items_query = base_query.limit(limit)
    top_items_rows = db.execute(top_items_query).all()
    
    # Process top items and fetch their names from AirFilter or StockItem
    top_items = []
    top_ids = []
    
    for row in top_items_rows:
        top_ids.append(row.product_id)
        
        # Fetch product name from AirFilter or StockItem
        product_name = None
        if row.category_id == 1:  # Assuming 1 is air_filter category
            air_filter = db.execute(
                select(AirFilter.part_number)
                .where(AirFilter.id == row.reference_id)
            ).scalar_one_or_none()
            product_name = air_filter if air_filter else f"Product {row.product_id}"
        elif row.category_id == 2:  # Assuming 2 is stock_item category
            stock_item = db.execute(
                select(StockItem.name)
                .where(StockItem.id == row.reference_id)
            ).scalar_one_or_none()
            product_name = stock_item if stock_item else f"Product {row.product_id}"
        else:
            product_name = f"Product {row.product_id}"
        
        top_items.append({
            "product_id": row.product_id,
            "product_name": product_name,
            "on_hand": row.on_hand,
            "available": row.available,
            "reserved": row.reserved,
            "ordered": row.ordered,
            "backordered": row.backordered,
            field: getattr(row, field) if field in ["on_hand", "reserved", "ordered"] else row.available if field == "available" else row.backordered
        })
    
    # Calculate "All Others" sum
    # Get sum of the field for all items not in top N
    all_others_sum = 0
    if top_ids:
        all_others_query = (
            select(func.coalesce(func.sum(field_expr), 0))
            .select_from(Quantity)
            .join(Product, Product.id == Quantity.product_id)
            .where(
                Quantity.warehouse_id == warehouse_id,
                Product.id.notin_(top_ids)
            )
        )
        all_others_sum = db.scalar(all_others_query) or 0
    else:
        # If no top items, sum all
        all_query = (
            select(func.coalesce(func.sum(field_expr), 0))
            .select_from(Quantity)
            .join(Product, Product.id == Quantity.product_id)
            .where(Quantity.warehouse_id == warehouse_id)
        )
        all_others_sum = db.scalar(all_query) or 0
    
    # Calculate total for percentage calculations
    total_query = (
        select(func.coalesce(func.sum(field_expr), 0))
        .select_from(Quantity)
        .join(Product, Product.id == Quantity.product_id)
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
