from flask import g, jsonify, request
from flask import Blueprint
from flask_jwt_extended import jwt_required
from sqlalchemy import select
from database.models import Supplier
from marshmallow import ValidationError
from app.api.Schemas.supplier_schema import SupplierSchema

supplier_bp = Blueprint('suppliers', __name__)
supplier_schema = SupplierSchema()
supplier_list_schema = SupplierSchema(many=True)

@supplier_bp.route('/suppliers', methods=['GET'])
@jwt_required()
def get_suppliers():
    db = g.db
    try:
        suppliers = db.execute(select(Supplier)).scalars().all()
        return jsonify(supplier_list_schema.dump(suppliers)), 200
    finally:
        db.close()

@supplier_bp.route('/suppliers/<int:id>', methods=['GET'])
@jwt_required()
def get_supplier(id):
    db = g.db
    try:
        supplier = db.execute(select(Supplier).where(Supplier.id == id)).scalars().first()

        if not supplier:
            return jsonify({"error": "Supplier not found"}), 404
        
        return jsonify(supplier_schema.dump(supplier)), 200
    finally:
        db.close()
    

@supplier_bp.route("/suppliers", methods=["POST"])
def create_supplier():
    db = g.db
    try:
        data = supplier_schema.load(request.get_json())
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    # Ensure name is unique
    existing = db.execute(
        select(Supplier).where(Supplier.name == data["name"])
    ).scalar_one_or_none()
    if existing:
        return jsonify({"error": "Supplier with this name already exists"}), 400

    new_supplier = Supplier.from_dict(data)
    db.add(new_supplier)
    db.commit()
    return jsonify(supplier_schema.dump(new_supplier)), 201

@supplier_bp.route("/suppliers/<int:id>", methods=["PATCH"])
def update_supplier(id):
    db = g.db
    supplier = db.execute(select(Supplier).where(Supplier.id == id)).scalars().first()
    if not supplier:
        return jsonify({"error": "Supplier not found"}), 404

    try:
        data = supplier_schema.load(request.get_json(), partial=True)
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    for key, value in data.items():
        setattr(supplier, key, value)

    db.commit()
    return jsonify(supplier_schema.dump(supplier)), 200

@supplier_bp.route("/suppliers/<int:id>", methods=["DELETE"])
def delete_supplier(id):
    db = g.db
    supplier = db.get(Supplier, id)

    if not supplier:
        return jsonify({"error": "Supplier not found"}), 404

    # 1️⃣ Check for Air Filters tied to this supplier
    if supplier.air_filters:
        return jsonify({
            "error": "Cannot delete supplier.",
            "reason": "Supplier is linked to Air Filters.",
            "linked_count": len(supplier.air_filters)
        }), 400

    # 2️⃣ Check for Orders tied to this supplier
    if supplier.orders:
        return jsonify({
            "error": "Cannot delete supplier.",
            "reason": "Supplier is linked to Orders.",
            "linked_count": len(supplier.orders)
        }), 400

    db.delete(supplier)
    db.commit()

    return jsonify({
        "message": "Supplier deleted successfully"
    }), 200