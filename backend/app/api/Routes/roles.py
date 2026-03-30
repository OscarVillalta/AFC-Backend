from flask import Blueprint, g, jsonify, request
from sqlalchemy import select

from database.models import Role, Permission, User
from app.api.tokens import permission_required

role_bp = Blueprint("roles", __name__)


# ==========================================
# ROLES ENDPOINTS
# ==========================================

@role_bp.route("/roles", methods=["GET"])
@permission_required("roles:manage")
def get_roles():
    db = g.db
    results = db.execute(select(Role)).scalars().all()
    return jsonify([
        {
            "id": r.id,
            "name": r.name,
            "description": r.description,
            "permissions": [p.name for p in r.permissions],
        }
        for r in results
    ]), 200


@role_bp.route("/roles", methods=["POST"])
@permission_required("roles:manage")
def create_role():
    db = g.db
    data = request.get_json() or {}
    name = data.get("name")
    description = data.get("description")
    permission_names = data.get("permissions", [])

    if not name:
        return jsonify({"error": "Role name is required"}), 400

    # Ensure role name is unique
    existing_role = db.execute(select(Role).where(Role.name == name)).scalar_one_or_none()
    if existing_role:
        return jsonify({"error": "Role already exists"}), 400

    new_role = Role(name=name, description=description)

    # Attach permissions if provided
    if permission_names:
        perms = db.execute(select(Permission).where(Permission.name.in_(permission_names))).scalars().all()
        new_role.permissions = perms

    db.add(new_role)
    db.commit()

    return jsonify({
        "id": new_role.id,
        "name": new_role.name,
        "description": new_role.description,
        "permissions": [p.name for p in new_role.permissions]
    }), 201


@role_bp.route("/roles/<int:id>", methods=["PATCH"])
@permission_required("roles:manage")
def update_role(id):
    db = g.db
    role = db.get(Role, id)
    if not role:
        return jsonify({"error": "Role not found"}), 404

    data = request.get_json() or {}

    if "name" in data:
        existing_role = db.execute(select(Role).where(Role.name == data["name"])).scalar_one_or_none()
        if existing_role and existing_role.id != id:
            return jsonify({"error": "Role name already in use"}), 400
        role.name = data["name"]

    if "description" in data:
        role.description = data["description"]

    # Update permissions by passing an array of permission name strings
    if "permissions" in data:
        permission_names = data["permissions"]
        perms = db.execute(select(Permission).where(Permission.name.in_(permission_names))).scalars().all()
        role.permissions = perms

    db.commit()

    return jsonify({
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "permissions": [p.name for p in role.permissions]
    }), 200


@role_bp.route("/roles/<int:id>", methods=["DELETE"])
@permission_required("roles:manage")
def delete_role(id):
    db = g.db
    role = db.get(Role, id)
    if not role:
        return jsonify({"error": "Role not found"}), 404

    # Safety Check 1: Prevent deleting the master Admin role
    if role.name == "Admin":
        return jsonify({"error": "Cannot delete the master Admin role"}), 400

    # Safety Check 2: Prevent deleting roles currently tied to users
    users_with_role = db.execute(select(User).where(User.role_id == id)).scalars().first()
    if users_with_role:
        return jsonify({"error": "Cannot delete role because it is currently assigned to active users. Reassign them first."}), 400

    db.delete(role)
    db.commit()

    return jsonify({"message": f"Role '{role.name}' deleted successfully"}), 200


# ==========================================
# PERMISSIONS ENDPOINTS
# ==========================================

@role_bp.route("/permissions", methods=["GET"])
@permission_required("roles:manage")
def get_permissions():
    db = g.db
    results = db.execute(select(Permission)).scalars().all()
    return jsonify([
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
        }
        for p in results
    ]), 200


@role_bp.route("/permissions", methods=["POST"])
@permission_required("roles:manage")
def create_permission():
    db = g.db
    data = request.get_json() or {}
    name = data.get("name")
    description = data.get("description")

    if not name:
        return jsonify({"error": "Permission name is required"}), 400

    existing = db.execute(select(Permission).where(Permission.name == name)).scalar_one_or_none()
    if existing:
        return jsonify({"error": "Permission already exists"}), 400

    new_perm = Permission(name=name, description=description)
    db.add(new_perm)
    db.commit()

    return jsonify({
        "id": new_perm.id,
        "name": new_perm.name,
        "description": new_perm.description
    }), 201


@role_bp.route("/permissions/<int:id>", methods=["DELETE"])
@permission_required("roles:manage")
def delete_permission(id):
    db = g.db
    perm = db.get(Permission, id)
    if not perm:
        return jsonify({"error": "Permission not found"}), 404

    db.delete(perm)
    db.commit()
    
    return jsonify({"message": "Permission deleted successfully"}), 200