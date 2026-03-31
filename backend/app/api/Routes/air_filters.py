from flask import g, jsonify, request, Blueprint
from flask_jwt_extended import jwt_required
from sqlalchemy import func, select, and_, or_
from database.models import AirFilter, AirFilterCategory, Supplier, Product, ProductCategory, Quantity, ChildProduct, Warehouse, OrderItem
from marshmallow import ValidationError
from app.api.Schemas.air_filters_schema import AirFilterSchema
from app.api.Schemas.air_filter_category_schema import AirFilterCategorySchema
from app.api.filters import _parse_stock_param, stock_level_filter
from app.api.tokens import permission_required

air_filter_bp = Blueprint("air_filters", __name__)
air_filter_schema = AirFilterSchema()
air_filter_category_schema = AirFilterCategorySchema(many=True)

ProductCategory_id = 1

# --- GET all Air Filters ---
@air_filter_bp.route("/air_filters", methods=["GET"])
@jwt_required()
def get_air_filters():
    db = g.db
    results = db.execute(select(AirFilter)).scalars().all()
    return jsonify([flt.to_dict(include_relationships=True) for flt in results]), 200


# --- GET air filter categories (id + name) ---
@air_filter_bp.route("/air_filter_categories", methods=["GET"])
@jwt_required()
def get_air_filter_categories():
    db = g.db
    categories = db.execute(select(AirFilterCategory)).scalars().all()
    return jsonify(air_filter_category_schema.dump(categories)), 200


# --- GET single Air Filter ---
@air_filter_bp.route("/air_filters/<int:id>", methods=["GET"])
@jwt_required()
def get_air_filter(id):
    db = g.db
    flt = db.get(AirFilter, id)
    if not flt:
        return jsonify({"error": "Air Filter not found"}), 404
    return jsonify(flt.to_dict(include_relationships=True)), 200


# --- POST new Air Filter ---
@air_filter_bp.route("/air_filters", methods=["POST"])
@permission_required("catalog:create")
def create_air_filter():
    db = g.db
    try:
        data = air_filter_schema.load(request.get_json())
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    supplier = db.get(Supplier, data["supplier_id"])
    if not supplier:
        return jsonify({"error": "Invalid supplier ID"}), 400
    
    category = db.get(AirFilterCategory, data["category_id"])
    if not category:
        return jsonify({"error": "Invalid category ID"}), 400

    # 1️⃣ Create AirFilter record
    new_filter = AirFilter.from_dict(data)
    db.add(new_filter)
    db.flush()

    product = Product(category_id=ProductCategory_id, reference_id=new_filter.id)
    db.add(product)
    db.flush()

    # 3️⃣ Create Quantity records for every warehouse
    warehouses = db.execute(select(Warehouse)).scalars().all()
    quantities = []
    for wh in warehouses:
        qty = Quantity(product_id=product.id, warehouse_id=wh.id, on_hand=0, reserved=0, ordered=0, location=0)
        db.add(qty)
        quantities.append(qty)
    db.flush()
    quantity_ids = [qty.id for qty in quantities]
    db.commit()

    return jsonify({
        "message": "Air Filter created successfully",
        "air_filter": new_filter.to_dict(include_relationships=True),
        "product_id": product.id,
        "quantity_ids": quantity_ids
    }), 201


# --- PATCH (partial update) ---
@air_filter_bp.route("/air_filters/<int:id>", methods=["PATCH"])
@permission_required("catalog:edit")
def update_air_filter(id):
    db = g.db
    flt = db.get(AirFilter, id)
    if not flt:
        return jsonify({"error": "Air Filter not found"}), 404

    try:
        data = air_filter_schema.load(request.get_json(), partial=True)
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    for key, value in data.items():
        setattr(flt, key, value)

    db.commit()
    return jsonify(air_filter_schema.dump(flt)), 200


# --- PUT (full replacement) ---
@air_filter_bp.route("/air_filters/<int:id>", methods=["PUT"])
@permission_required("catalog:edit")
def replace_air_filter(id):
    db = g.db
    flt = db.get(AirFilter, id)
    if not flt:
        return jsonify({"error": "Air Filter not found"}), 404

    try:
        data = air_filter_schema.load(request.get_json())
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    for key, value in data.items():
        setattr(flt, key, value)

    db.commit()
    return jsonify(air_filter_schema.dump(flt)), 200


# --- DELETE ---
@air_filter_bp.route("/air_filters/<int:id>", methods=["DELETE"])
@permission_required("catalog:archive")
def delete_air_filter(id):
    db = g.db
    flt = db.get(AirFilter, id)
    if not flt:
        return jsonify({"error": "Air Filter not found"}), 404

    # Cascade delete linked product + quantity
    if flt.product:
        db.delete(flt.product)
    # Also delete linked child product if exists
    if flt.child_product:
        db.delete(flt.child_product)
    db.delete(flt)
    db.commit()
    return jsonify({"message": "Air Filter deleted successfully."}), 200


# =====================================================
# 🔎 Search Air Filters
# =====================================================
@air_filter_bp.route("/air_filters/search", methods=["GET"])
@jwt_required()
def search_air_filters():
    db = g.db

    # --- Query parameters ---
    part_number = request.args.get("part_number")
    description = request.args.get("description")
    supplier_name = request.args.get("supplier")
    merv = request.args.get("merv", type=int)
    height = request.args.get("height", type=int)
    width = request.args.get("width", type=int)
    depth = request.args.get("depth", type=int)
    category = request.args.get("category")
    location = request.args.get("location", type=int)
    status = request.args.get("status")
    on_hand, on_hand_cmp = _parse_stock_param("on_hand")
    reserved, reserved_cmp = _parse_stock_param("reserved")
    available, available_cmp = _parse_stock_param("available")
    ordered, ordered_cmp = _parse_stock_param("ordered")
    back_ordered, back_ordered_cmp = _parse_stock_param("back_ordered")
    min_backordered = request.args.get("backordered", type=int)
    has_orders = request.args.get("has_orders", "").lower() == "true"

    # Pagination
    page = request.args.get("page", default=1, type=int)
    limit = request.args.get("limit", default=25, type=int)
    offset = (page - 1) * limit

    # --- Base Query ---
    # Note: Using OUTER JOINs to include AirFilters even if they lack associated
    # Product/ChildProduct/Quantity records (which shouldn't happen in normal operation,
    # but allows for debugging orphaned records). Items without quantity data will have
    # NULL values in those fields.
    query = (
        select(
            AirFilter.id,
            AirFilter.part_number,
            AirFilter.description,
            AirFilter.merv_rating,
            AirFilter.height,
            AirFilter.width,
            AirFilter.depth,
            Product.id.label("product_id"),
            ChildProduct.id.label("child_product_id"),
            ChildProduct.parent_product_id.label("parent_product_id"),
            Supplier.name.label("supplier_name"),
            AirFilterCategory.name.label("filter_category"),

            Quantity.on_hand,
            Quantity.reserved,
            Quantity.ordered,
            Quantity.location,
            Quantity.available,
            Quantity.backordered,

            
        )
        .join(Supplier, AirFilter.supplier_id == Supplier.id)
        .join(AirFilterCategory, AirFilter.category_id == AirFilterCategory.id)
        .outerjoin(Product, and_(Product.category_id == 1, Product.reference_id == AirFilter.id))
        .outerjoin(ChildProduct, and_(ChildProduct.category_id == 1, ChildProduct.reference_id == AirFilter.id))
        .outerjoin(Quantity, and_(
            or_(Quantity.product_id == Product.id, Quantity.product_id == ChildProduct.parent_product_id),
            Quantity.warehouse_id == g.active_warehouse_id
        ))
        .distinct(AirFilter.id)
    )

    # --- Dynamic Filters ---
    filters = []

    if part_number:
        filters.append(AirFilter.part_number.ilike(f"%{part_number}%"))
    if description:
        filters.append(AirFilter.description.ilike(f"%{description}%"))
    if supplier_name:
        filters.append(Supplier.name.ilike(f"%{supplier_name}%"))
    if merv is not None:
        filters.append(AirFilter.merv_rating == merv)
    if height is not None:
        filters.append(AirFilter.height == height)
    if width is not None:
        filters.append(AirFilter.width == width)
    if depth is not None:
        filters.append(AirFilter.depth == depth)
    if category:
        filters.append(AirFilterCategory.name.ilike(f"%{category}%"))
    if location is not None:
        filters.append(Quantity.location == location)
    if on_hand is not None:
        filters.append(stock_level_filter(Quantity.on_hand, on_hand, on_hand_cmp))
    if reserved is not None:
        filters.append(stock_level_filter(Quantity.reserved, reserved, reserved_cmp))
    if available is not None:
        filters.append(stock_level_filter(Quantity.available, available, available_cmp))
    if ordered is not None:
        filters.append(stock_level_filter(Quantity.ordered, ordered, ordered_cmp))
    if back_ordered is not None:
        filters.append(stock_level_filter(Quantity.backordered, back_ordered, back_ordered_cmp))
    if min_backordered is not None:
        filters.append(Quantity.backordered >= min_backordered)
    if has_orders:
        order_item_exists = select(OrderItem.id).where(
            or_(
                OrderItem.product_id == Product.id,
                OrderItem.child_product_id == ChildProduct.id,
            )
        ).correlate(Product, ChildProduct).exists()
        filters.append(order_item_exists)

    # --- Status filter ---
    if status == "low_stock":
        filters.append(Quantity.available <= 10)
    elif status == "backordered":
        filters.append(Quantity.backordered > 0)
    elif status == "has_orders":
        filters.append(Quantity.ordered > 0)

    if filters:
        query = query.where(and_(*filters))

    # --- Total Count ---
    total = len(db.execute(query).mappings().all())

    # --- Pagination ---
    query = query.limit(limit).offset(offset)

    # --- Execute ---
    results = db.execute(query).mappings().all()
    results = [dict(row) for row in results]

    return jsonify({
        "page": page,
        "limit": limit,
        "count": len(results),
        "total": total,
        "results": results
    }), 200
