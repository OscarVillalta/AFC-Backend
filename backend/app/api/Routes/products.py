from pprint import pp
from flask import g, jsonify, request, Blueprint
from sqlalchemy import select, and_, or_
from sqlalchemy.orm import selectinload, with_loader_criteria
from database.models import (
    Product, ProductCategory, AirFilter, StockItem, Media,
    Quantity, Supplier, ChildProduct,
)
from app.api.Schemas.product_schema import ProductSchema

product_bp = Blueprint("products", __name__)
product_schema = ProductSchema()
product_list_schema = ProductSchema(many=True)

# =====================================================
# 🔹 GET all products (joined data: Air + Misc + Quantity)
# =====================================================
@product_bp.route("/products", methods=["GET"])
def get_products():
    db = g.db

    # Use selectinload to minimize round-trips
    results = db.execute(
        select(Product)
        .options(
            selectinload(Product.category),
            selectinload(Product.quantities),
            selectinload(Product.air_filter).selectinload(AirFilter.supplier),
            selectinload(Product.stock_item).selectinload(StockItem.supplier),
            selectinload(Product.media).selectinload(Media.supplier)
        )
    ).scalars().all()

    response = []
    for p in results:
        category = p.category.name if p.category else "Unknown"
        quantity = p.quantity.to_dict() if p.quantity else {}

        # --- Determine which subtable applies ---
        if p.air_filter:
            details = p.air_filter.to_dict()
            details["supplier_name"] = p.air_filter.supplier.name if p.air_filter.supplier else None
        elif p.stock_item:
            details = p.stock_item.to_dict()
            details["supplier_name"] = p.stock_item.supplier.name if p.stock_item.supplier else None
        elif p.media:
            details = p.media.to_dict()
            details["supplier_name"] = p.media.supplier.name if p.media.supplier else None
        else:
            details = {}

        response.append({
            "id": p.id,
            "category": category,
            "reference_id": p.reference_id,
            "details": details,
            "quantity": quantity
        })

    return jsonify(response), 200


# =====================================================
# 🔹 GET single product (joined)
# =====================================================
@product_bp.route("/products/<int:id>", methods=["GET"])
def get_product(id):
    db = g.db

    warehouse_id = g.active_warehouse_id

    product = db.execute(
        select(Product)
        .where(Product.id == id)
        .options(
            with_loader_criteria(Quantity, Quantity.warehouse_id == warehouse_id),
            selectinload(Product.category),
            selectinload(Product.quantities),
            selectinload(Product.air_filter).selectinload(AirFilter.supplier),
            selectinload(Product.stock_item).selectinload(StockItem.supplier),
            selectinload(Product.media).selectinload(Media.supplier),
            selectinload(Product.child_products).selectinload(ChildProduct.air_filter).selectinload(AirFilter.supplier),
            selectinload(Product.child_products).selectinload(ChildProduct.stock_item).selectinload(StockItem.supplier)
        )
    ).scalars().first()
            
    if not product:
        return jsonify({"error": "Product not found"}), 404

    category = product.category.name if product.category else "Unknown"

    if product.quantity:
        quantity = product.quantity.to_dict()
        quantity["available"] = product.quantity.available
        quantity["backordered"] = product.quantity.backordered
    

    if product.air_filter:
        details = product.air_filter.to_dict()
        details["supplier_name"] = product.air_filter.supplier.name if product.air_filter.supplier else None
    elif product.stock_item:
        details = product.stock_item.to_dict()
        details["supplier_name"] = product.stock_item.supplier.name if product.stock_item.supplier else None
    elif product.media:
        details = product.media.to_dict()
        details["supplier_name"] = product.media.supplier.name if product.media.supplier else None
    else:
        details = {}

    # Include child products
    child_products_data = []
    for child in product.child_products:
        if child.is_active:
            child_category = child.category.name if child.category else "Unknown"
            if child.air_filter:
                child_details = child.air_filter.to_dict()
                child_details["supplier_name"] = child.air_filter.supplier.name if child.air_filter.supplier else None
            elif child.stock_item:
                child_details = child.stock_item.to_dict()
                child_details["supplier_name"] = child.stock_item.supplier.name if child.stock_item.supplier else None
            else:
                child_details = {}
            
            child_products_data.append({
                "id": child.id,
                "category": child_category,
                "reference_id": child.reference_id,
                "details": child_details
            })

    return jsonify({
        "id": product.id,
        "category": category,
        "reference_id": product.reference_id,
        "details": details,
        "quantity": quantity,
        "child_products": child_products_data
    }), 200


@product_bp.route("/products/<int:id>/archive", methods=["PATCH"])
def archive_product(id):
    db = g.db
    product = db.get(Product, id)

    if not product:
        return jsonify({"error": "Product not found"}), 404

    if not product.is_active:
        return jsonify({"message": "Product already archived"}), 200

    # Soft delete
    product.is_active = False
    db.commit()

    return jsonify({"message": "Product archived successfully"}), 200

@product_bp.route("/products/<int:id>", methods=["DELETE"])
def delete_product(id):
    return jsonify({"error": "Products cannot be deleted. Archive instead."}), 409

# =====================================================
# 🔹 GET all product names (for searches and such)
# =====================================================
@product_bp.route("/products/names", methods=["GET"])
def get_products_names():
    db = g.db

    # Use selectinload to minimize round-trips
    results = db.execute(
        select(Product)
        .options(
            selectinload(Product.category),
            selectinload(Product.quantities),
            selectinload(Product.air_filter).selectinload(AirFilter.supplier),
            selectinload(Product.stock_item).selectinload(StockItem.supplier)
        )
    ).scalars().all()

    response = []
    for p in results:
        category = p.category.name if p.category else "Unknown"

        # --- Determine which subtable applies ---
        if p.category.name == "Air Filters":
            details = p.air_filter.to_dict()["part_number"]
        elif p.category.name == "Stock Items":
            details = p.stock_item.to_dict()["name"]
        else:
            details = None

        response.append({
            "id": p.id,
            "category": category,
            "part_number": details,
        })

    return jsonify(response), 200


# =====================================================
# 🔎 Search Products
# =====================================================
@product_bp.route("/products/search", methods=["GET"])
def search_products():
    from sqlalchemy import func

    db = g.db

    # --- Query parameters ---
    category = request.args.get("category")
    name = request.args.get("name")
    supplier = request.args.get("supplier")
    location = request.args.get("location", type=int)
    is_active = request.args.get("is_active")

    # Pagination
    page = request.args.get("page", default=1, type=int)
    limit = request.args.get("limit", default=25, type=int)
    offset = (page - 1) * limit

    # --- Aliased supplier tables for each subtable join ---
    supplier_air = Supplier.__table__.alias("supplier_air")
    supplier_stock = Supplier.__table__.alias("supplier_stock")
    supplier_media = Supplier.__table__.alias("supplier_media")

    # --- Base Query ---
    query = (
        select(
            Product.id,
            Product.reference_id,
            Product.is_active,
            ProductCategory.name.label("category"),

            AirFilter.part_number.label("air_filter_part_number"),
            StockItem.name.label("stock_item_name"),
            Media.part_number.label("media_part_number"),

            func.coalesce(
                AirFilter.part_number,
                StockItem.name,
                Media.part_number,
            ).label("name"),

            func.coalesce(
                supplier_air.c.name,
                supplier_stock.c.name,
                supplier_media.c.name,
            ).label("supplier_name"),

            Quantity.on_hand,
            Quantity.reserved,
            Quantity.ordered,
            Quantity.location,
            Quantity.available,
            Quantity.backordered,
        )
        .join(ProductCategory, Product.category_id == ProductCategory.id)
        .outerjoin(AirFilter, and_(Product.category_id == 1, Product.reference_id == AirFilter.id))
        .outerjoin(supplier_air, AirFilter.supplier_id == supplier_air.c.id)
        .outerjoin(StockItem, and_(Product.category_id == 3, Product.reference_id == StockItem.id))
        .outerjoin(supplier_stock, StockItem.supplier_id == supplier_stock.c.id)
        .outerjoin(Media, and_(Product.category_id == 4, Product.reference_id == Media.id))
        .outerjoin(supplier_media, Media.supplier_id == supplier_media.c.id)
        .outerjoin(Quantity, and_(
            Quantity.product_id == Product.id,
            Quantity.warehouse_id == g.active_warehouse_id,
        ))
        .distinct(Product.id)
    )

    # --- Dynamic Filters ---
    filters = []

    if category:
        filters.append(ProductCategory.name.ilike(f"%{category}%"))
    if name:
        filters.append(
            or_(
                AirFilter.part_number.ilike(f"%{name}%"),
                StockItem.name.ilike(f"%{name}%"),
                Media.part_number.ilike(f"%{name}%"),
            )
        )
    if supplier:
        filters.append(
            or_(
                supplier_air.c.name.ilike(f"%{supplier}%"),
                supplier_stock.c.name.ilike(f"%{supplier}%"),
                supplier_media.c.name.ilike(f"%{supplier}%"),
            )
        )
    if location is not None:
        filters.append(Quantity.location == location)
    if is_active is not None:
        filters.append(Product.is_active == (is_active.lower() in ("true", "1", "yes")))

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
        "results": results,
    }), 200
