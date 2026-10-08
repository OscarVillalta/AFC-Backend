from __future__ import annotations

from flask import g, jsonify, request, Blueprint
from flask_jwt_extended import get_jwt, verify_jwt_in_request
from sqlalchemy import and_, case, desc, func, select
from sqlalchemy.orm import aliased, selectinload
from database.models import AirFilter, Media, Product, Quantity, StockItem, Supplier

ADMIN_ROLE = "admin"


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


_AIR_FILTER_CATEGORY_ID = 1
_STOCK_ITEM_CATEGORY_ID = 3
_MEDIA_CATEGORY_ID = 4
_MAX_VALUE_GROUPS = 200
_GROUP_BY_ALIASES = {
    "": None,
    "total": None,
    "all": None,
    "supplier": "supplier",
    "suppliers": "supplier",
    "provider": "supplier",
    "providers": "supplier",
    "product": "product",
    "products": "product",
}


def _as_int(value) -> int:
    if value is None:
        return 0
    return int(value)


def _as_money(value) -> float:
    if value is None:
        return 0.0
    return round(float(value), 2)


def _as_price(value):
    if value is None:
        return None
    return round(float(value), 2)


def _contains_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _parse_positive_int(name: str):
    raw = request.args.get(name, default=None, type=str)
    if raw is None or raw.strip() == "":
        return None, None
    try:
        value = int(raw.strip())
    except ValueError:
        return None, f"{name} must be an integer"
    if value < 1:
        return None, f"{name} must be a positive integer"
    return value, None


def _inventory_value_permitted() -> bool:
    """Only the Admin role may see dollar totals. Missing or invalid tokens are rejected."""
    verify_jwt_in_request()
    claims = get_jwt() or {}
    role = claims.get("role")
    return isinstance(role, str) and role.strip().lower() == ADMIN_ROLE


def _inventory_lines(warehouse_id: int):
    """One row per quantity in the warehouse.

    Gross value is on-hand times unit price. A missing unit price contributes
    zero and is reported separately. Child products share the parent quantity
    row, so they are not added again.
    """
    supplier_for_air = aliased(Supplier)
    supplier_for_stock = aliased(Supplier)
    supplier_for_media = aliased(Supplier)

    return (
        select(
            Product.id.label("product_id"),
            func.coalesce(
                AirFilter.part_number,
                StockItem.name,
                Media.part_number,
                func.concat("Product ", Product.id),
            ).label("product_name"),
            func.coalesce(
                AirFilter.supplier_id,
                StockItem.supplier_id,
                Media.supplier_id,
            ).label("supplier_id"),
            func.coalesce(
                supplier_for_air.name,
                supplier_for_stock.name,
                supplier_for_media.name,
            ).label("supplier_name"),
            func.coalesce(Quantity.on_hand, 0).label("on_hand"),
            Product.unit_price.label("unit_price"),
            (
                func.coalesce(Quantity.on_hand, 0) * func.coalesce(Product.unit_price, 0.0)
            ).label("gross"),
        )
        .select_from(Quantity)
        .join(Product, Product.id == Quantity.product_id)
        .outerjoin(
            AirFilter,
            and_(Product.category_id == _AIR_FILTER_CATEGORY_ID, Product.reference_id == AirFilter.id),
        )
        .outerjoin(supplier_for_air, AirFilter.supplier_id == supplier_for_air.id)
        .outerjoin(
            StockItem,
            and_(Product.category_id == _STOCK_ITEM_CATEGORY_ID, Product.reference_id == StockItem.id),
        )
        .outerjoin(supplier_for_stock, StockItem.supplier_id == supplier_for_stock.id)
        .outerjoin(
            Media,
            and_(Product.category_id == _MEDIA_CATEGORY_ID, Product.reference_id == Media.id),
        )
        .outerjoin(supplier_for_media, Media.supplier_id == supplier_for_media.id)
        .where(Quantity.warehouse_id == warehouse_id)
    )


def build_inventory_value(
    db,
    warehouse_id: int,
    *,
    supplier_id: int | None = None,
    product_id: int | None = None,
    group_by: str | None = None,
    q: str | None = None,
    limit: int = 100,
) -> dict:
    lines = _inventory_lines(warehouse_id).subquery("inventory_lines")
    filters = []
    if supplier_id is not None:
        filters.append(lines.c.supplier_id == supplier_id)
    if product_id is not None:
        filters.append(lines.c.product_id == product_id)
    if q:
        filters.append(lines.c.product_name.ilike(_contains_pattern(q), escape="\\"))

    unpriced = func.coalesce(
        func.sum(
            case(
                (and_(lines.c.unit_price.is_(None), lines.c.on_hand != 0), 1),
                else_=0,
            )
        ),
        0,
    )
    totals = select(
        func.coalesce(func.sum(lines.c.gross), 0).label("gross_total"),
        func.coalesce(func.sum(lines.c.on_hand), 0).label("on_hand_units"),
        func.count(lines.c.product_id).label("sku_count"),
        unpriced.label("unpriced_skus"),
    ).select_from(lines)
    if filters:
        totals = totals.where(and_(*filters))

    row = db.execute(totals).mappings().one()

    groups = []
    truncated = False
    if group_by == "supplier":
        gross_sum = func.coalesce(func.sum(lines.c.gross), 0)
        grouped = (
            select(
                lines.c.supplier_id,
                lines.c.supplier_name,
                gross_sum.label("gross_total"),
                func.coalesce(func.sum(lines.c.on_hand), 0).label("on_hand_units"),
                func.count(lines.c.product_id).label("sku_count"),
            )
            .select_from(lines)
            .group_by(lines.c.supplier_id, lines.c.supplier_name)
            .order_by(desc(gross_sum), lines.c.supplier_name)
        )
        if filters:
            grouped = grouped.where(and_(*filters))
        found = db.execute(grouped.limit(limit + 1)).mappings().all()
        truncated = len(found) > limit
        groups = [
            {
                "supplier_id": item["supplier_id"],
                "supplier_name": item["supplier_name"] or "Unknown provider",
                "product_id": None,
                "product_name": None,
                "unit_price": None,
                "on_hand_units": _as_int(item["on_hand_units"]),
                "sku_count": _as_int(item["sku_count"]),
                "gross_total": _as_money(item["gross_total"]),
            }
            for item in found[:limit]
        ]
    elif group_by == "product":
        gross_sum = func.coalesce(func.sum(lines.c.gross), 0)
        grouped = (
            select(
                lines.c.product_id,
                lines.c.product_name,
                lines.c.supplier_id,
                lines.c.supplier_name,
                func.max(lines.c.unit_price).label("unit_price"),
                func.coalesce(func.sum(lines.c.on_hand), 0).label("on_hand_units"),
                func.count(lines.c.product_id).label("sku_count"),
                gross_sum.label("gross_total"),
            )
            .select_from(lines)
            .group_by(
                lines.c.product_id,
                lines.c.product_name,
                lines.c.supplier_id,
                lines.c.supplier_name,
            )
            .order_by(desc(gross_sum), lines.c.product_name)
        )
        if filters:
            grouped = grouped.where(and_(*filters))
        found = db.execute(grouped.limit(limit + 1)).mappings().all()
        truncated = len(found) > limit
        groups = [
            {
                "supplier_id": item["supplier_id"],
                "supplier_name": item["supplier_name"] or "Unknown provider",
                "product_id": item["product_id"],
                "product_name": item["product_name"] or f"Product {item['product_id']}",
                "unit_price": _as_price(item["unit_price"]),
                "on_hand_units": _as_int(item["on_hand_units"]),
                "sku_count": _as_int(item["sku_count"]),
                "gross_total": _as_money(item["gross_total"]),
            }
            for item in found[:limit]
        ]

    return {
        "restricted": False,
        "gross_total": _as_money(row["gross_total"]),
        "on_hand_units": _as_int(row["on_hand_units"]),
        "sku_count": _as_int(row["sku_count"]),
        "unpriced_skus": _as_int(row["unpriced_skus"]),
        "supplier_id": supplier_id,
        "product_id": product_id,
        "group_by": group_by,
        "groups": groups,
        "groups_truncated": truncated,
    }


@inventory_stats_bp.route("/inventory/value", methods=["GET"])
def get_inventory_value():
    """Sum of gross inventory value (on-hand x unit price) for the active warehouse.

    Admins only. Anyone else gets 403 and no dollar amounts.
    """
    if not _inventory_value_permitted():
        return jsonify({"error": "Forbidden"}), 403

    supplier_id, supplier_error = _parse_positive_int("supplier_id")
    if supplier_error:
        return jsonify({"error": supplier_error}), 400
    product_id, product_error = _parse_positive_int("product_id")
    if product_error:
        return jsonify({"error": product_error}), 400

    group_raw = (request.args.get("group_by") or "").strip().lower()
    if group_raw not in _GROUP_BY_ALIASES:
        return jsonify({"error": "group_by must be supplier or product"}), 400
    group_by = _GROUP_BY_ALIASES[group_raw]

    limit = 100
    if group_by:
        limit, limit_error = _parse_positive_int("limit")
        if limit_error:
            return jsonify({"error": limit_error}), 400
        if limit is None:
            limit = 100
        if limit > _MAX_VALUE_GROUPS:
            return jsonify({"error": f"limit must be between 1 and {_MAX_VALUE_GROUPS}"}), 400

    q = (request.args.get("q") or "").strip()
    if len(q) > 80:
        q = q[:80]

    warehouse_id = g.active_warehouse_id
    payload = build_inventory_value(
        g.db,
        warehouse_id,
        supplier_id=supplier_id,
        product_id=product_id,
        group_by=group_by,
        q=q or None,
        limit=limit,
    )
    return jsonify(payload), 200
