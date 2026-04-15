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
LOW_STOCK_THRESHOLD = 10


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
    use_current_warehouse = request.args.get("use_current_warehouse", "true").lower() == "true"

    # Pagination
    page = request.args.get("page", default=1, type=int)
    limit = request.args.get("limit", default=25, type=int)
    offset = (page - 1) * limit

    # --- Quantity columns & join condition for parent products only ---
    if use_current_warehouse:
        qty_join_cond = and_(Quantity.product_id == Product.id, Quantity.warehouse_id == g.active_warehouse_id)
        q_on_hand = Quantity.on_hand
        q_reserved = Quantity.reserved
        q_ordered = Quantity.ordered
        q_location = Quantity.location
        q_available = Quantity.available
        q_backordered = Quantity.backordered
    else:
        qty_join_cond = Quantity.product_id == Product.id
        q_on_hand = func.coalesce(func.sum(Quantity.on_hand), 0).label("on_hand")
        q_reserved = func.coalesce(func.sum(Quantity.reserved), 0).label("reserved")
        q_ordered = func.coalesce(func.sum(Quantity.ordered), 0).label("ordered")
        q_location = func.min(Quantity.location).label("location")
        q_available = func.greatest(
            func.coalesce(func.sum(Quantity.on_hand), 0) - func.coalesce(func.sum(Quantity.reserved), 0), 0
        ).label("available")
        q_backordered = func.greatest(
            func.coalesce(func.sum(Quantity.reserved), 0) - func.coalesce(func.sum(Quantity.on_hand), 0), 0
        ).label("backordered")

    # --- Base Query for Parent Products Only ---
    base_columns = [
        Media.id,
        Media.part_number,
        Media.description,
        Media.length,
        Media.width,
        Media.unit_of_measure,
        Product.id.label("product_id"),
        Supplier.name.label("supplier_name"),
        MediaCategory.name.label("media_category"),
    ]

    query = (
        select(*base_columns, q_on_hand, q_reserved, q_ordered, q_location, q_available, q_backordered)
        .join(Supplier, Media.supplier_id == Supplier.id)
        .join(MediaCategory, Media.category_id == MediaCategory.id)
        .join(Product, and_(Product.category_id == ProductCategory_id, Product.reference_id == Media.id))
        .outerjoin(Quantity, qty_join_cond)
    )

    if use_current_warehouse:
        query = query.distinct(Media.id)
    else:
        query = query.group_by(*base_columns)

    # --- Dynamic Filters ---
    filters = []
    qty_filters = []

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
        qty_filters.append(q_location == location)
    if on_hand is not None:
        qty_filters.append(stock_level_filter(q_on_hand, on_hand, on_hand_cmp))
    if reserved is not None:
        qty_filters.append(stock_level_filter(q_reserved, reserved, reserved_cmp))
    if available is not None:
        qty_filters.append(stock_level_filter(q_available, available, available_cmp))
    if ordered is not None:
        qty_filters.append(stock_level_filter(q_ordered, ordered, ordered_cmp))
    if back_ordered is not None:
        qty_filters.append(stock_level_filter(q_backordered, back_ordered, back_ordered_cmp))
    if min_backordered is not None:
        qty_filters.append(q_backordered >= min_backordered)
    if has_orders:
        order_item_exists = select(OrderItem.id).where(
            OrderItem.product_id == Product.id
        ).correlate(Product).exists()
        filters.append(order_item_exists)

    # --- Status filter ---
    if status == "low_stock":
        qty_filters.append(q_available <= LOW_STOCK_THRESHOLD)
    elif status == "backordered":
        qty_filters.append(q_backordered > 0)
    elif status == "has_orders":
        qty_filters.append(q_ordered > 0)

    if filters:
        query = query.where(and_(*filters))
    if qty_filters:
        if use_current_warehouse:
            query = query.where(and_(*qty_filters))
        else:
            query = query.having(and_(*qty_filters))

    # --- Total Count ---
    total = len(db.execute(query).mappings().all())

    # --- Pagination ---
    query = query.limit(limit).offset(offset)

    # --- Execute ---
    parent_results = db.execute(query).mappings().all()
    parent_results = [dict(row) for row in parent_results]

    # --- Fetch children for each parent ---
    parent_product_ids = [row["product_id"] for row in parent_results if row.get("product_id")]
    
    # Query for children with their quantities
    if parent_product_ids:
        qty_product_cond_child = Quantity.product_id == ChildProduct.parent_product_id
        
        if use_current_warehouse:
            qty_join_cond_child = and_(qty_product_cond_child, Quantity.warehouse_id == g.active_warehouse_id)
            c_on_hand = Quantity.on_hand
            c_reserved = Quantity.reserved
            c_ordered = Quantity.ordered
            c_location = Quantity.location
            c_available = Quantity.available
            c_backordered = Quantity.backordered
        else:
            qty_join_cond_child = qty_product_cond_child
            c_on_hand = func.coalesce(func.sum(Quantity.on_hand), 0).label("on_hand")
            c_reserved = func.coalesce(func.sum(Quantity.reserved), 0).label("reserved")
            c_ordered = func.coalesce(func.sum(Quantity.ordered), 0).label("ordered")
            c_location = func.min(Quantity.location).label("location")
            c_available = func.greatest(
                func.coalesce(func.sum(Quantity.on_hand), 0) - func.coalesce(func.sum(Quantity.reserved), 0), 0
            ).label("available")
            c_backordered = func.greatest(
                func.coalesce(func.sum(Quantity.reserved), 0) - func.coalesce(func.sum(Quantity.on_hand), 0), 0
            ).label("backordered")

        child_columns = [
            Media.id,
            Media.part_number,
            Media.description,
            Media.length,
            Media.width,
            Media.unit_of_measure,
            ChildProduct.id.label("child_product_id"),
            ChildProduct.parent_product_id.label("parent_product_id"),
            Supplier.name.label("supplier_name"),
            MediaCategory.name.label("media_category"),
        ]

        children_query = (
            select(*child_columns, c_on_hand, c_reserved, c_ordered, c_location, c_available, c_backordered)
            .join(Supplier, Media.supplier_id == Supplier.id)
            .join(MediaCategory, Media.category_id == MediaCategory.id)
            .join(ChildProduct, and_(ChildProduct.category_id == ProductCategory_id, ChildProduct.reference_id == Media.id))
            .outerjoin(Quantity, qty_join_cond_child)
            .where(ChildProduct.parent_product_id.in_(parent_product_ids))
        )

        if use_current_warehouse:
            children_query = children_query.distinct(ChildProduct.id)
        else:
            children_query = children_query.group_by(*child_columns)

        children_results = db.execute(children_query).mappings().all()
        children_results = [dict(row) for row in children_results]

        # Group children by parent_product_id
        children_by_parent = {}
        for child in children_results:
            parent_id = child.pop("parent_product_id")
            if parent_id not in children_by_parent:
                children_by_parent[parent_id] = []
            children_by_parent[parent_id].append(child)

        # Add children to each parent
        for parent in parent_results:
            parent["children"] = children_by_parent.get(parent.get("product_id"), [])
    else:
        # No parents, so no children
        for parent in parent_results:
            parent["children"] = []

    return jsonify({
        "page": page,
        "limit": limit,
        "count": len(parent_results),
        "total": total,
        "results": parent_results
    }), 200
