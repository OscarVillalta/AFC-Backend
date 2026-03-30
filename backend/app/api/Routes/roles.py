from flask import Blueprint, g, jsonify
from sqlalchemy import select

from database.models import Role, Permission
from app.api.tokens import permission_required

role_bp = Blueprint("roles", __name__)


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
