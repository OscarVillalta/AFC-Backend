from flask import g, jsonify, request, Blueprint
from sqlalchemy import func, select, and_, or_
from database.models import AirFilter, AirFilterCategory, Supplier, Product, ProductCategory, Quantity, ChildProduct, Warehouse
from marshmallow import ValidationError
from app.api.Schemas.air_filters_schema import AirFilterSchema
from app.api.Schemas.air_filter_category_schema import AirFilterCategorySchema

air_filter_bp = Blueprint("air_filters", __name__)
air_filter_schema = AirFilterSchema()
air_filter_category_schema = AirFilterCategorySchema(many=True)

ProductCategory_id = 1

# --- GET all Air Filters ---
@air_filter_bp.route("/air_filters", methods=["GET"])
def get_air_filters():
    db = g.db
    results = db.execute(select(AirFilter)).scalars().all()
    return jsonify([flt.to_dict(include_relationships=True) for flt in results]), 200


# --- GET air filter categories (id + name) ---
@air_filter_bp.route("/air_filter_categories", methods=["GET"])
def get_air_filter_categories():
    db = g.db
    categories = db.execute(select(AirFilterCategory)).scalars().all()
    return jsonify(air_filter_category_schema.dump(categories)), 200


# --- GET single Air Filter ---
@air_filter_bp.route("/air_filters/<int:id>", methods=["GET"])
def get_air_filter(id):
    db = g.db
    flt = db.get(AirFilter, id)
    if not flt:
        return jsonify({"error": "Air Filter not found"}), 404
    return jsonify(flt.to_dict(include_relationships=True)), 200


# --- POST new Air Filter ---
@air_filter_bp.route("/air_filters", methods=["POST"])
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
    quantity_ids = []
    for wh in warehouses:
        qty = Quantity(product_id=product.id, warehouse_id=wh.id, on_hand=0, reserved=0, ordered=0, location=0)
        db.add(qty)
        db.flush()
        quantity_ids.append(qty.id)
    db.commit()

    return jsonify({
        "message": "Air Filter created successfully",
        "air_filter": new_filter.to_dict(include_relationships=True),
        "product_id": product.id,
        "quantity_ids": quantity_ids
    }), 201


# --- PATCH (partial update) ---
@air_filter_bp.route("/air_filters/<int:id>", methods=["PATCH"])
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
        .outerjoin(Quantity, or_(Quantity.product_id == Product.id, Quantity.product_id == ChildProduct.parent_product_id))
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
