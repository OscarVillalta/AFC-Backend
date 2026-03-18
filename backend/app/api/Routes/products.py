from pprint import pp
from flask import g, jsonify, Blueprint
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from database.models import Product, ProductCategory, AirFilter, StockItem, Quantity, Supplier, ChildProduct
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
    warehouse_id = g.active_warehouse_id

    # Use selectinload to minimize round-trips
    # Filter products to those with a quantity record in the active warehouse
    results = db.execute(
        select(Product)
        .join(Quantity, (Quantity.product_id == Product.id) & (Quantity.warehouse_id == warehouse_id))
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
        # Use the quantity record for the active warehouse
        warehouse_qty = next((q for q in p.quantities if q.warehouse_id == warehouse_id), None)
        quantity = warehouse_qty.to_dict() if warehouse_qty else {}

        # --- Determine which subtable applies ---
        if p.air_filter:
            details = p.air_filter.to_dict()
            details["supplier_name"] = p.air_filter.supplier.name if p.air_filter.supplier else None
        elif p.stock_item:
            details = p.stock_item.to_dict()
            details["supplier_name"] = p.stock_item.supplier.name if p.stock_item.supplier else None
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

    product = db.execute(
        select(Product)
        .where(Product.id == id)
        .options(
            selectinload(Product.category),
            selectinload(Product.quantities),
            selectinload(Product.air_filter).selectinload(AirFilter.supplier),
            selectinload(Product.stock_item).selectinload(StockItem.supplier),
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
