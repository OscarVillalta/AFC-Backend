from flask import Blueprint, g, jsonify, request
from sqlalchemy import select
from marshmallow import ValidationError
from flask_jwt_extended import get_jwt_identity

from database.models import User, UserRole
from backend.app.api.tokens import role_required
from backend.app.api.Schemas.user_schema import UserSchema

user_bp = Blueprint("users", __name__)
user_schema = UserSchema()
user_list_schema = UserSchema(many=True)

ADMIN = UserRole.ADMIN.value


# --- GET all users ---
@user_bp.route("/users", methods=["GET"])
@role_required(ADMIN)
def get_users():
    db = g.db
    results = db.execute(select(User)).scalars().all()
    return jsonify(user_list_schema.dump(results)), 200


# --- GET single user ---
@user_bp.route("/users/<int:id>", methods=["GET"])
@role_required(ADMIN)
def get_user(id):
    db = g.db
    user = db.get(User, id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify(user_schema.dump(user)), 200


# --- POST create user ---
@user_bp.route("/users", methods=["POST"])
@role_required(ADMIN)
def create_user():
    db = g.db

    try:
        data = user_schema.load(request.get_json() or {})
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    # Check for duplicate email
    existing = db.execute(
        select(User).where(User.email == data["email"])
    ).scalar_one_or_none()
    if existing:
        return jsonify({"error": "A user with this email already exists"}), 400

    user = User(
        email=data["email"],
        role_id=data["role_id"],
    )
    user.set_password(data["password"])

    db.add(user)
    db.commit()
    return jsonify(user_schema.dump(user)), 201


# --- PATCH update user ---
@user_bp.route("/users/<int:id>", methods=["PATCH"])
@role_required(ADMIN)
def update_user(id):
    db = g.db
    user = db.get(User, id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    try:
        data = user_schema.load(request.get_json() or {}, partial=True)
    except ValidationError as err:
        return jsonify({"errors": err.messages}), 400

    # If email is being updated, check uniqueness
    if "email" in data and data["email"] != user.email:
        existing = db.execute(
            select(User).where(User.email == data["email"])
        ).scalar_one_or_none()
        if existing:
            return jsonify({"error": "A user with this email already exists"}), 400
        user.email = data["email"]

    if "role_id" in data:
        # Prevent admin from changing their own role
        if str(user.id) == get_jwt_identity() and data["role_id"] != user.role_id:
            return jsonify({"error": "Cannot change your own role"}), 400
        user.role_id = data["role_id"]

    if "password" in data:
        user.set_password(data["password"])

    db.commit()
    return jsonify(user_schema.dump(user)), 200


# --- DELETE user ---
@user_bp.route("/users/<int:id>", methods=["DELETE"])
@role_required(ADMIN)
def delete_user(id):
    db = g.db
    user = db.get(User, id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    # Prevent admin from deleting themselves
    if str(user.id) == get_jwt_identity():
        return jsonify({"error": "Cannot delete your own account"}), 400

    db.delete(user)
    db.commit()
    return jsonify({"message": "User deleted"}), 200
