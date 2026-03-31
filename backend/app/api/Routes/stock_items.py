from flask import g, jsonify, request, Blueprint
from flask_jwt_extended import jwt_required
from sqlalchemy import select, and_, or_
from marshmallow import ValidationError
from database.models import StockItem, StockItemCategory, Supplier, Product, Quantity, ChildProduct, Warehouse, OrderItem
from app.api.Schemas.stock_item_schema import StockItemSchema
from app.api.Schemas.stock_item_category_schema import StockItemCategorySchema
from app.api.filters import _parse_stock_param, stock_level_filter
from app.api.tokens import permission_required

stock_item_bp = Blueprint("stock_items", __name__)
stock_item_schema = StockItemSchema()
stock_item_list_schema = StockItemSchema(many=True)
stock_item_category_schema = StockItemCategorySchema(many=True)

product_category = 3
LOW_STOCK_THRESHOLD = 10

# =====================================================
# 🔹 GET all stock item categories
# =====================================================
@stock_item_bp.route("/stock_item_categories", methods=["GET"])
@jwt_required()
def get_stock_item_categories():
    db = g.db
    categories = db.execute(select(StockItemCategory)).scalars().all()
    return jsonify(stock_item_category_schema.dump(categories)), 200


# =====================================================
# 🔹 GET all stock items
# =====================================================
@stock_item_bp.route("/stock_items", methods=["GET"])
@jwt_required()
def get_stock_items():
    db = g.db
    results = db.execute(select(StockItem)).scalars().all()
    return jsonify(stock_item_list_schema.dump(results)), 200


# =====================================================
# 🔹 GET single stock item
# =====================================================
@stock_item_bp.route("/stock_items/<int:id>", methods=["GET"])
@jwt_required()
def get_stock_item(id):
    db = g.db
    item = db.execute(select(StockItem).where(StockItem.id == id)).scalars().first()
    if not item:
        return jsonify({"error": "Stock item not found"}), 404
    return jsonify(stock_item_schema.dump(item)), 200


# =====================================================
# 🔹 POST new stock item
# =====================================================
@stock_item_bp.route("/stock_items", methods=["POST"])
@permission_required("catalog:create")
def create_stock_item():
    db = g.db

    try:
        data = stock_item_schema.load(request.get_json())
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    # Check that supplier exists
    supplier = db.execute(
        select(Supplier).where(Supplier.id == data["supplier_id"])
    ).scalar_one_or_none()
    if not supplier:
        return jsonify({"error": "Supplier not found"}), 404

    # Check that category exists
    category = db.execute(
        select(StockItemCategory).where(StockItemCategory.id == data["category_id"])
    ).scalar_one_or_none()
    if not category:
        return jsonify({"error": "Stock item category not found"}), 404

    # Check name uniqueness
    existing = db.execute(
        select(StockItem).where(StockItem.name == data["name"])
    ).scalar_one_or_none()
    if existing:
        return jsonify({"error": "A stock item with this name already exists"}), 409

    # Create the StockItem
    stock_item = StockItem.from_dict(data)
    db.add(stock_item)
    db.flush()

    new_product = Product(
        category_id=product_category,
        reference_id=stock_item.id
    )
    db.add(new_product)
    db.flush()

    # Create Quantity records for every warehouse
    warehouses = db.execute(select(Warehouse)).scalars().all()
    quantities = []
    for wh in warehouses:
        qty = Quantity(product_id=new_product.id, warehouse_id=wh.id, on_hand=0, reserved=0, ordered=0, location=0)
        db.add(qty)
        quantities.append(qty)
    db.flush()
    quantity_ids = [qty.id for qty in quantities]
    db.commit()

    return jsonify({
        "message": "Stock item created successfully.",
        "stock_item": stock_item_schema.dump(stock_item),
        "product_id": new_product.id,
        "quantity_ids": quantity_ids
    }), 201


# =====================================================
# 🔹 PATCH (partial update)
# =====================================================
@stock_item_bp.route("/stock_items/<int:id>", methods=["PATCH"])
@permission_required("catalog:edit")
def update_stock_item(id):
    db = g.db
    item = db.execute(select(StockItem).where(StockItem.id == id)).scalars().first()
    if not item:
        return jsonify({"error": "Stock item not found"}), 404

    try:
        data = stock_item_schema.load(request.get_json(), partial=True)
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    # Check name uniqueness if name is being updated
    if "name" in data and data["name"] != item.name:
        existing = db.execute(
            select(StockItem).where(StockItem.name == data["name"])
        ).scalar_one_or_none()
        if existing:
            return jsonify({"error": "A stock item with this name already exists"}), 409

    for key, value in data.items():
        setattr(item, key, value)

    db.commit()
    return jsonify(stock_item_schema.dump(item)), 200


# =====================================================
# 🔹 DELETE
# =====================================================
@stock_item_bp.route("/stock_items/<int:id>", methods=["DELETE"])
@permission_required("catalog:archive")
def delete_stock_item(id):
    db = g.db
    item = db.execute(select(StockItem).where(StockItem.id == id)).scalars().first()
    if not item:
        return jsonify({"error": "Stock item not found"}), 404

    # Delete associated product if exists
    product = db.execute(
        select(Product).where(
            and_(Product.reference_id == id, Product.category_id == product_category)
        )
    ).scalar_one_or_none()
    if product:
        db.delete(product)

    # Delete associated child product if exists
    child_product = db.execute(
        select(ChildProduct).where(
            and_(ChildProduct.reference_id == id, ChildProduct.category_id == product_category)
        )
    ).scalar_one_or_none()
    if child_product:
        db.delete(child_product)

    db.delete(item)
    db.commit()

    return jsonify({"message": "Stock item and related product deleted successfully."}), 200


# =====================================================
# 🔹 SEARCH stock items (with filters & pagination)
# =====================================================
@stock_item_bp.route("/stock_items/search", methods=["GET"])
@jwt_required()
def search_stock_items():
    db = g.db

    # --- Query parameters ---
    name = request.args.get("name")
    description = request.args.get("description")
    supplier_name = request.args.get("supplier")
    category_name = request.args.get("category")
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
            StockItem.id,
            StockItem.name,
            StockItem.description,
            Product.id.label("product_id"),
            ChildProduct.id.label("child_product_id"),
            ChildProduct.parent_product_id.label("parent_product_id"),
            Supplier.name.label("supplier_name"),
            StockItemCategory.name.label("category_name"),

            Quantity.on_hand,
            Quantity.reserved,
            Quantity.ordered,
            Quantity.location,
            Quantity.available,
            Quantity.backordered,
        )
        .join(Supplier, StockItem.supplier_id == Supplier.id)
        .join(StockItemCategory, StockItem.category_id == StockItemCategory.id)
        .outerjoin(Product, and_(Product.reference_id == StockItem.id, Product.category_id == product_category))
        .outerjoin(ChildProduct, and_(ChildProduct.reference_id == StockItem.id, ChildProduct.category_id == product_category))
        .outerjoin(Quantity, and_(
            or_(Quantity.product_id == Product.id, Quantity.product_id == ChildProduct.parent_product_id),
            Quantity.warehouse_id == g.active_warehouse_id
        ))
        .distinct(StockItem.id)
    )

    # --- Dynamic Filters ---
    filters = []

    if name:
        filters.append(StockItem.name.ilike(f"%{name}%"))
    if description:
        filters.append(StockItem.description.ilike(f"%{description}%"))
    if supplier_name:
        filters.append(Supplier.name.ilike(f"%{supplier_name}%"))
    if category_name:
        filters.append(StockItemCategory.name.ilike(f"%{category_name}%"))
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
        filters.append(Quantity.available <= LOW_STOCK_THRESHOLD)
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
