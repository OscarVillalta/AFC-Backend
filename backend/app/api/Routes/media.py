from flask import g, jsonify, request, Blueprint
from flask_jwt_extended import jwt_required
from sqlalchemy import func, select, and_, or_
from database.models import Media, MediaCategory, Supplier, Product, ProductCategory, Quantity, ChildProduct, Warehouse, OrderItem
from marshmallow import ValidationError
from app.api.Schemas.media_schema import MediaSchema, MediaCategorySchema
from app.api.filters import _parse_stock_param, stock_level_filter
from app.api.tokens import permission_required

media_bp = Blueprint("media", __name__)
media_schema = MediaSchema()
media_category_schema = MediaCategorySchema(many=True)

ProductCategory_id = 4


# --- GET all Media ---
@media_bp.route("/media", methods=["GET"])
@jwt_required()
def get_media():
    db = g.db
    results = db.execute(select(Media)).scalars().all()
    return jsonify([item.to_dict(include_relationships=True) for item in results]), 200


# --- GET media categories (id + name) ---
@media_bp.route("/media_categories", methods=["GET"])
@jwt_required()
def get_media_categories():
    db = g.db
    categories = db.execute(select(MediaCategory)).scalars().all()
    return jsonify(media_category_schema.dump(categories)), 200


# --- GET single Media item ---
@media_bp.route("/media/<int:id>", methods=["GET"])
@jwt_required()
def get_media_item(id):
    db = g.db
    item = db.get(Media, id)
    if not item:
        return jsonify({"error": "Media item not found"}), 404
    return jsonify(item.to_dict(include_relationships=True)), 200


# --- POST new Media item ---
@media_bp.route("/media", methods=["POST"])
@permission_required("catalog:create")
def create_media():
    db = g.db
    try:
        data = media_schema.load(request.get_json())
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    supplier = db.get(Supplier, data["supplier_id"])
    if not supplier:
        return jsonify({"error": "Invalid supplier ID"}), 400

    category = db.get(MediaCategory, data["category_id"])
    if not category:
        return jsonify({"error": "Invalid category ID"}), 400

    # 1️⃣ Create Media record
    new_media = Media.from_dict(data)
    db.add(new_media)
    db.flush()

    # 2️⃣ Create Product record
    product = Product(category_id=ProductCategory_id, reference_id=new_media.id)
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
        "message": "Media item created successfully",
        "media": new_media.to_dict(include_relationships=True),
        "product_id": product.id,
        "quantity_ids": quantity_ids
    }), 201


# --- PATCH (partial update) ---
@media_bp.route("/media/<int:id>", methods=["PATCH"])
@permission_required("catalog:edit")
def update_media(id):
    db = g.db
    item = db.get(Media, id)
    if not item:
        return jsonify({"error": "Media item not found"}), 404

    try:
        data = media_schema.load(request.get_json(), partial=True)
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    for key, value in data.items():
        setattr(item, key, value)

    db.commit()
    return jsonify(media_schema.dump(item)), 200


# --- PUT (full replacement) ---
@media_bp.route("/media/<int:id>", methods=["PUT"])
@permission_required("catalog:edit")
def replace_media(id):
    db = g.db
    item = db.get(Media, id)
    if not item:
        return jsonify({"error": "Media item not found"}), 404

    try:
        data = media_schema.load(request.get_json())
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    for key, value in data.items():
        setattr(item, key, value)

    db.commit()
    return jsonify(media_schema.dump(item)), 200


# --- DELETE ---
@media_bp.route("/media/<int:id>", methods=["DELETE"])
@permission_required("catalog:archive")
def delete_media(id):
    db = g.db
    item = db.get(Media, id)
    if not item:
        return jsonify({"error": "Media item not found"}), 404

    # Cascade delete linked product + quantity
    if item.product:
        db.delete(item.product)
    # Also delete linked child product if exists
    if item.child_product:
        db.delete(item.child_product)
    db.delete(item)
    db.commit()
    return jsonify({"message": "Media item deleted successfully."}), 200


# =====================================================
# 🔎 Search Media
# =====================================================
@media_bp.route("/media/search", methods=["GET"])
@jwt_required()
def search_media():
    db = g.db

    # --- Query parameters ---
    part_number = request.args.get("part_number")
    description = request.args.get("description")
    supplier_name = request.args.get("supplier")
    length = request.args.get("length", type=float)
    width = request.args.get("width", type=float)
    unit_of_measure = request.args.get("unit_of_measure")
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
    query = (
        select(
            Media.id,
            Media.part_number,
            Media.description,
            Media.length,
            Media.width,
            Media.unit_of_measure,
            Product.id.label("product_id"),
            ChildProduct.id.label("child_product_id"),
            ChildProduct.parent_product_id.label("parent_product_id"),
            Supplier.name.label("supplier_name"),
            MediaCategory.name.label("media_category"),

            Quantity.on_hand,
            Quantity.reserved,
            Quantity.ordered,
            Quantity.location,
            Quantity.available,
            Quantity.backordered,
        )
        .join(Supplier, Media.supplier_id == Supplier.id)
        .join(MediaCategory, Media.category_id == MediaCategory.id)
        .outerjoin(Product, and_(Product.category_id == ProductCategory_id, Product.reference_id == Media.id))
        .outerjoin(ChildProduct, and_(ChildProduct.category_id == ProductCategory_id, ChildProduct.reference_id == Media.id))
        .outerjoin(Quantity, and_(
            or_(Quantity.product_id == Product.id, Quantity.product_id == ChildProduct.parent_product_id),
            Quantity.warehouse_id == g.active_warehouse_id
        ))
        .distinct(Media.id)
    )

    # --- Dynamic Filters ---
    filters = []

    if part_number:
        filters.append(Media.part_number.ilike(f"%{part_number}%"))
    if description:
        filters.append(Media.description.ilike(f"%{description}%"))
    if supplier_name:
        filters.append(Supplier.name.ilike(f"%{supplier_name}%"))
    if length is not None:
        filters.append(Media.length == length)
    if width is not None:
        filters.append(Media.width == width)
    if unit_of_measure:
        filters.append(Media.unit_of_measure.ilike(f"%{unit_of_measure}%"))
    if category:
        filters.append(MediaCategory.name.ilike(f"%{category}%"))
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
