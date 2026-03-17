from flask import g, jsonify, request, Blueprint
from sqlalchemy import select
from marshmallow import ValidationError
from database.models import ChildProduct, Product, AirFilter, StockItem, StockItemCategory, ProductCategory, Supplier, AirFilterCategory
from app.api.Schemas.child_product_schema import ChildProductSchema

child_product_bp = Blueprint("child_products", __name__)
child_product_schema = ChildProductSchema()
child_product_list_schema = ChildProductSchema(many=True)

# Category constants
CATEGORY_AIR_FILTERS = 1
CATEGORY_STOCK_ITEMS = 3

# =====================================================
# 🔹 GET all Child Products
# =====================================================
@child_product_bp.route("/child_products", methods=["GET"])
def get_child_products():
    db = g.db
    results = db.execute(select(ChildProduct)).scalars().all()
    return jsonify([cp.to_dict(include_relationships=True) for cp in results]), 200


# =====================================================
# 🔹 GET single Child Product
# =====================================================
@child_product_bp.route("/child_products/<int:id>", methods=["GET"])
def get_child_product(id):
    db = g.db
    
    from sqlalchemy.orm import selectinload
    
    cp = db.execute(
        select(ChildProduct)
        .where(ChildProduct.id == id)
        .options(
            selectinload(ChildProduct.category),
            selectinload(ChildProduct.air_filter).selectinload(AirFilter.supplier),
            selectinload(ChildProduct.stock_item).selectinload(StockItem.supplier),
            selectinload(ChildProduct.parent_product).selectinload(Product.air_filter).selectinload(AirFilter.supplier),
            selectinload(ChildProduct.parent_product).selectinload(Product.stock_item).selectinload(StockItem.supplier),
            selectinload(ChildProduct.parent_product).selectinload(Product.quantities)
        )
    ).scalars().first()
    
    if not cp:
        return jsonify({"error": "Child product not found"}), 404
    
    category = cp.category.name if cp.category else "Unknown"
    
    # Get child product details
    if cp.air_filter:
        details = cp.air_filter.to_dict()
        details["supplier_name"] = cp.air_filter.supplier.name if cp.air_filter.supplier else None
    elif cp.stock_item:
        details = cp.stock_item.to_dict()
        details["supplier_name"] = cp.stock_item.supplier.name if cp.stock_item.supplier else None
    else:
        details = {}
    
    # Get parent product information
    parent_data = None
    if cp.parent_product:
        parent_category = cp.parent_product.category.name if cp.parent_product.category else "Unknown"
        if cp.parent_product.air_filter:
            parent_details = cp.parent_product.air_filter.to_dict()
            parent_details["supplier_name"] = cp.parent_product.air_filter.supplier.name if cp.parent_product.air_filter.supplier else None
        elif cp.parent_product.stock_item:
            parent_details = cp.parent_product.stock_item.to_dict()
            parent_details["supplier_name"] = cp.parent_product.stock_item.supplier.name if cp.parent_product.stock_item.supplier else None
        else:
            parent_details = {}
        
        parent_data = {
            "id": cp.parent_product.id,
            "category": parent_category,
            "category_id": cp.parent_product.category_id,
            "reference_id": cp.parent_product.reference_id,
            "details": parent_details
        }
    
    # Get quantity (shared with parent)
    quantity_data = cp.quantity.to_dict() if cp.quantity else None
    if cp.quantity:
        quantity_data["available"] = cp.quantity.available
        quantity_data["backordered"] = cp.quantity.backordered
    
    return jsonify({
        "id": cp.id,
        "category": category,
        "reference_id": cp.reference_id,
        "details": details,
        "quantity": quantity_data,
        "parent_product": parent_data
    }), 200


# =====================================================
# 🔹 POST new Child Product (Air Filter)
# =====================================================
@child_product_bp.route("/child_products/air_filters", methods=["POST"])
def create_child_air_filter():
    """Create a child product that references an air filter and shares parent's quantity"""
    db = g.db
    data = request.get_json() or {}
    
    # Required fields
    required = ["part_number", "supplier_id", "category_id", "parent_product_id"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"{field} is required"}), 400
    
    # Validate parent product exists and has quantity
    parent_product = db.get(Product, data["parent_product_id"])
    if not parent_product:
        return jsonify({"error": "Parent product not found"}), 400
    if not parent_product.quantity:
        return jsonify({"error": "Parent product does not have a quantity record"}), 400
    
    # Validate supplier and category
    supplier = db.get(Supplier, data["supplier_id"])
    if not supplier:
        return jsonify({"error": "Invalid supplier ID"}), 400
    
    category = db.get(AirFilterCategory, data["category_id"])
    if not category:
        return jsonify({"error": "Invalid air filter category ID"}), 400
    
    # Create the air filter record
    air_filter_data = {
        "part_number": data["part_number"],
        "supplier_id": data["supplier_id"],
        "category_id": data["category_id"],
        "merv_rating": data.get("merv_rating", 0),
        "height": data.get("height", 0),
        "width": data.get("width", 0),
        "depth": data.get("depth", 1)
    }
    new_filter = AirFilter.from_dict(air_filter_data)
    db.add(new_filter)
    db.flush()
    
    # Create the child product
    child_product = ChildProduct(
        category_id=CATEGORY_AIR_FILTERS,
        reference_id=new_filter.id,
        parent_product_id=data["parent_product_id"]
    )
    db.add(child_product)
    db.commit()
    
    return jsonify({
        "message": "Child air filter product created successfully",
        "child_product_id": child_product.id,
        "air_filter": new_filter.to_dict(),
        "parent_product_id": child_product.parent_product_id
    }), 201


# =====================================================
# 🔹 POST new Child Product (Stock Item)
# =====================================================
@child_product_bp.route("/child_products/stock_item", methods=["POST"])
def create_child_stock_item():
    """Create a child product that references a stock item and shares parent's quantity"""
    db = g.db
    data = request.get_json() or {}

    # Required fields
    required = ["name", "supplier_id", "category_id", "parent_product_id"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"{field} is required"}), 400

    # Validate parent product exists and has quantity
    parent_product = db.get(Product, data["parent_product_id"])
    if not parent_product:
        return jsonify({"error": "Parent product not found"}), 400
    if not parent_product.quantity:
        return jsonify({"error": "Parent product does not have a quantity record"}), 400

    # Validate supplier
    supplier = db.get(Supplier, data["supplier_id"])
    if not supplier:
        return jsonify({"error": "Invalid supplier ID"}), 400

    # Validate stock item category
    category = db.get(StockItemCategory, data["category_id"])
    if not category:
        return jsonify({"error": "Invalid stock item category ID"}), 400

    # Create the stock item record
    stock_item_data = {
        "name": data["name"],
        "supplier_id": data["supplier_id"],
        "category_id": data["category_id"],
        "description": data.get("description", "")
    }
    new_stock_item = StockItem.from_dict(stock_item_data)
    db.add(new_stock_item)
    db.flush()

    # Create the child product
    child_product = ChildProduct(
        category_id=CATEGORY_STOCK_ITEMS,
        reference_id=new_stock_item.id,
        parent_product_id=data["parent_product_id"]
    )
    db.add(child_product)
    db.commit()

    return jsonify({
        "message": "Child stock item product created successfully",
        "child_product_id": child_product.id,
        "stock_item": new_stock_item.to_dict(),
        "parent_product_id": child_product.parent_product_id
    }), 201


# =====================================================
# 🔹 DELETE Child Product
# =====================================================
@child_product_bp.route("/child_products/<int:id>", methods=["DELETE"])
def delete_child_product(id):
    db = g.db
    cp = db.get(ChildProduct, id)
    if not cp:
        return jsonify({"error": "Child product not found"}), 404
    
    # Delete the associated air filter or stock item
    if cp.air_filter:
        db.delete(cp.air_filter)
    elif cp.stock_item:
        db.delete(cp.stock_item)
    
    db.delete(cp)
    db.commit()
    
    return jsonify({"message": "Child product deleted successfully"}), 200
