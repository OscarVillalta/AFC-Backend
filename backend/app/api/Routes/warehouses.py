from flask import g, jsonify, request, Blueprint
from sqlalchemy import select
from database.models import Warehouse
from marshmallow import ValidationError
from app.api.Schemas.warehouse_schema import WarehouseSchema

warehouse_bp = Blueprint("warehouses", __name__)
warehouse_schema = WarehouseSchema()
warehouse_list_schema = WarehouseSchema(many=True)


# --- GET all warehouses ---
@warehouse_bp.route("/warehouses", methods=["GET"])
def get_warehouses():
    db = g.db
    results = db.execute(select(Warehouse)).scalars().all()
    return jsonify(warehouse_list_schema.dump(results)), 200


# --- GET single warehouse ---
@warehouse_bp.route("/warehouses/<int:id>", methods=["GET"])
def get_warehouse(id):
    db = g.db
    warehouse = db.get(Warehouse, id)
    if not warehouse:
        return jsonify({"error": "Warehouse not found"}), 404
    return jsonify(warehouse_schema.dump(warehouse)), 200


# --- POST new warehouse ---
@warehouse_bp.route("/warehouses", methods=["POST"])
def create_warehouse():
    db = g.db
    try:
        data = warehouse_schema.load(request.get_json() or {})
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    # Ensure name is unique
    existing = db.execute(
        select(Warehouse).where(Warehouse.name == data["name"])
    ).scalar_one_or_none()
    if existing:
        return jsonify({"error": f"Warehouse with name '{data['name']}' already exists"}), 400

    warehouse = Warehouse(**data)
    db.add(warehouse)
    db.commit()
    return jsonify(warehouse_schema.dump(warehouse)), 201


# --- PATCH warehouse ---
@warehouse_bp.route("/warehouses/<int:id>", methods=["PATCH"])
def update_warehouse(id):
    db = g.db
    warehouse = db.get(Warehouse, id)
    if not warehouse:
        return jsonify({"error": "Warehouse not found"}), 404

    try:
        data = warehouse_schema.load(request.get_json() or {}, partial=True)
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    for key, value in data.items():
        setattr(warehouse, key, value)

    db.commit()
    return jsonify(warehouse_schema.dump(warehouse)), 200
