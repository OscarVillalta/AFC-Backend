from pprint import pp
from flask import g, jsonify, request, Blueprint
from flask_jwt_extended import jwt_required
from sqlalchemy import select, and_, or_
from sqlalchemy.orm import selectinload, with_loader_criteria
from database.models import (
    Product, ProductCategory, AirFilter, StockItem, Media,
    Quantity, Supplier, ChildProduct,
)
from app.api.Schemas.product_schema import ProductSchema
from app.api.Schemas.product_migration_schema import ProductMigrationSchema
from app.api.tokens import permission_required
from app.api.error_handling import (
    safe_commit,
    ResourceNotFoundError,
    InvalidInputError,
    DuplicateResourceError,
    handle_database_error,
)
from app.services.product_migration_service import migrate_product_catalog_type
from marshmallow import ValidationError
from sqlalchemy.exc import IntegrityError, DatabaseError

product_bp = Blueprint("products", __name__)
product_schema = ProductSchema()
product_list_schema = ProductSchema(many=True)
product_migration_schema = ProductMigrationSchema()


def _serialize_product_detail(db, product: Product, warehouse_id: int) -> dict:
    category = product.category.name if product.category else "Unknown"
    quantity = {}
    qty = next((q for q in product.quantities if q.warehouse_id == warehouse_id), None)
    if qty:
        quantity = qty.to_dict()
        quantity["available"] = qty.available
        quantity["backordered"] = qty.backordered

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

    child_products_data = []
    for child in product.child_products:
        if not child.is_active:
            continue
        child_category = child.category.name if child.category else "Unknown"
        if child.air_filter:
            child_details = child.air_filter.to_dict()
            child_details["supplier_name"] = child.air_filter.supplier.name if child.air_filter.supplier else None
        elif child.stock_item:
            child_details = child.stock_item.to_dict()
            child_details["supplier_name"] = child.stock_item.supplier.name if child.stock_item.supplier else None
        elif child.media:
            child_details = child.media.to_dict()
            child_details["supplier_name"] = child.media.supplier.name if child.media.supplier else None
        else:
            child_details = {}

        child_products_data.append({
            "id": child.id,
            "category": child_category,
            "reference_id": child.reference_id,
            "details": child_details,
        })

    return {
        "id": product.id,
        "category": category,
        "reference_id": product.reference_id,
        "default_no_stock_deduction": product.default_no_stock_deduction,
        "details": details,
        "quantity": quantity,
        "child_products": child_products_data,
    }

# =====================================================
# 🔹 GET all products (joined data: Air + Misc + Quantity)
# =====================================================
@product_bp.route("/products", methods=["GET"])
@jwt_required()
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
@jwt_required()
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
        "default_no_stock_deduction": product.default_no_stock_deduction,
        "details": details,
        "quantity": quantity,
        "child_products": child_products_data
    }), 200


@product_bp.route("/products/<int:id>", methods=["PATCH"])
@permission_required("catalog:edit")
def patch_product(id):
    db = g.db
    product = db.get(Product, id)

    if not product:
        return jsonify({"error": "Product not found"}), 404

    data = request.get_json() or {}

    if "default_no_stock_deduction" in data:
        value = data["default_no_stock_deduction"]
        if not isinstance(value, bool):
            return jsonify({"error": "default_no_stock_deduction must be a boolean"}), 400
        product.default_no_stock_deduction = value

    error = safe_commit(db)
    if error:
        from app.api.error_handling import handle_database_error
        return handle_database_error(error)

    return jsonify({
        "id": product.id,
        "default_no_stock_deduction": product.default_no_stock_deduction,
    }), 200


@product_bp.route("/products/<int:id>/migrate", methods=["POST"])
@permission_required("catalog:edit")
def migrate_product(id: int):
    db = g.db
    warehouse_id = g.active_warehouse_id

    try:
        data = product_migration_schema.load(request.get_json() or {})
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    try:
        product = migrate_product_catalog_type(
            db,
            id,
            target_type=data["target_type"],
            target_category_id=data["target_category_id"],
            overrides=data.get("overrides"),
            child_overrides=data.get("child_overrides"),
        )
        error = safe_commit(db, "migrating product catalog type")
        if error:
            return handle_database_error(error)

        product = db.execute(
            select(Product)
            .where(Product.id == product.id)
            .options(
                selectinload(Product.category),
                selectinload(Product.quantities),
                selectinload(Product.air_filter).selectinload(AirFilter.supplier),
                selectinload(Product.stock_item).selectinload(StockItem.supplier),
                selectinload(Product.media).selectinload(Media.supplier),
                selectinload(Product.child_products).selectinload(ChildProduct.air_filter).selectinload(AirFilter.supplier),
                selectinload(Product.child_products).selectinload(ChildProduct.stock_item).selectinload(StockItem.supplier),
                selectinload(Product.child_products).selectinload(ChildProduct.media).selectinload(Media.supplier),
            )
        ).unique().scalar_one()

        return jsonify({
            "message": "Product migrated successfully.",
            "product": _serialize_product_detail(db, product, warehouse_id),
        }), 200
    except (ResourceNotFoundError, InvalidInputError, DuplicateResourceError) as e:
        db.rollback()
        return jsonify(e.to_dict()), e.status_code
    except IntegrityError as e:
        db.rollback()
        return handle_database_error(e, "migrating product catalog type")
    except DatabaseError as e:
        db.rollback()
        return handle_database_error(e, "migrating product catalog type")


@product_bp.route("/products/<int:id>/archive", methods=["PATCH"])
@permission_required("catalog:archive")
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
@permission_required("catalog:archive")
def delete_product(id):
    return jsonify({"error": "Products cannot be deleted. Archive instead."}), 409

# =====================================================
# 🔹 GET all product names (for searches and such)
# =====================================================
@product_bp.route("/products/names", methods=["GET"])
@jwt_required()
def get_products_names():
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

        # --- Determine which subtable applies ---
        if p.category.name == "Air Filters":
            details = p.air_filter.to_dict()["part_number"]
        elif p.category.name == "Stock Items":
            details = p.stock_item.to_dict()["name"] if p.stock_item else "Unknown Stock Item"
        elif p.category.name == "Media Items":
            details = p.media.to_dict()["part_number"] if p.media else None
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
@jwt_required()
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
