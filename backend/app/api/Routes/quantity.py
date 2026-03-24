from flask import g, jsonify, request, Blueprint
from sqlalchemy import select
from database.models import Quantity, Product, Warehouse
from marshmallow import ValidationError
from app.api.Schemas.quantity_schema import QuantitySchema

quantity_bp = Blueprint("quantities", __name__)
quantity_schema = QuantitySchema()

# --- GET all quantities (filtered by active warehouse) ---
@quantity_bp.route("/quantities", methods=["GET"])
def get_quantities():
    db = g.db
    warehouse_id = g.active_warehouse_id
    results = db.execute(
        select(Quantity).where(Quantity.warehouse_id == warehouse_id)
    ).scalars().all()
    return jsonify([qty.to_dict() for qty in results]), 200


# --- GET single quantity ---
@quantity_bp.route("/quantities/<int:id>", methods=["GET"])
def get_quantity(id):
    db = g.db
    qty = db.get(Quantity, id)
    if not qty:
        return jsonify({"error": "Quantity not found"}), 404
    return jsonify(qty.to_dict()), 200


# --- POST new quantity ---
@quantity_bp.route("/quantities", methods=["POST"])
def create_quantity():
    """
    Create a new quantity record for a Product in the active warehouse.
    
    Note: Child products do not have their own quantity records.
    They share the parent product's quantity via the ChildProduct.quantity property.
    """
    db = g.db
    warehouse_id = g.active_warehouse_id
    try:
        data = quantity_schema.load(request.get_json())
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    # Check product exists
    product_exists = db.execute(
        select(Product.id).where(Product.id == data["product_id"])
    ).scalar_one_or_none()
    if not product_exists:
        return jsonify({"error": "Product does not exist"}), 400

    # Check warehouse exists
    warehouse_exists = db.execute(
        select(Warehouse.id).where(Warehouse.id == warehouse_id)
    ).scalar_one_or_none()
    if not warehouse_exists:
        return jsonify({"error": f"Warehouse {warehouse_id} does not exist"}), 400

    # Ensure product doesn't already have a quantity for this warehouse
    existing_qty = db.execute(
        select(Quantity).where(
            (Quantity.product_id == data["product_id"]) &
            (Quantity.warehouse_id == warehouse_id)
        )
    ).scalars().first()
    if existing_qty:
        return jsonify({
            "error": f"Quantity already exists for product_id {data['product_id']} in warehouse {warehouse_id}"
        }), 400

    new_qty = Quantity.from_dict(data)
    new_qty.warehouse_id = warehouse_id
    db.add(new_qty)
    db.commit()
    return jsonify(quantity_schema.dump(new_qty)), 201


# --- PATCH (partial update) ---
@quantity_bp.route("/quantities/<int:id>", methods=["PATCH"])
def update_quantity(id):
    db = g.db
    qty = db.get(Quantity, id)
    if not qty:
        return jsonify({"error": "Quantity not found"}), 404

    try:
        data = quantity_schema.load(request.get_json(), partial=True)
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    for key, value in data.items():
        setattr(qty, key, value)

    db.commit()
    return jsonify(quantity_schema.dump(qty)), 200


# --- PUT (full replacement) ---
@quantity_bp.route("/quantities/<int:id>", methods=["PUT"])
def replace_quantity(id):
    db = g.db
    qty = db.get(Quantity, id)
    if not qty:
        return jsonify({"error": "Quantity not found"}), 404

    try:
        data = quantity_schema.load(request.get_json())
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    for key, value in data.items():
        setattr(qty, key, value)

    db.commit()
    return jsonify(quantity_schema.dump(qty)), 200


# --- DELETE ---
@quantity_bp.route("/quantities/<int:id>", methods=["DELETE"])
def delete_quantity(id):
    db = g.db
    qty = db.get(Quantity, id)
    if not qty:
        return jsonify({"error": "Quantity not found"}), 404

    db.delete(qty)
    db.commit()
    return jsonify({"message": "Quantity deleted successfully"}), 200
