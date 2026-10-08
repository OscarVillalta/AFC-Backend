import operator
from functools import wraps
from flask import jsonify, request
from flask_jwt_extended import jwt_required, get_jwt


PRICE_MANAGE_PERMISSION = "price:manage"


def has_permission(permission: str) -> bool:
    """Check the current request's JWT for a permission. Requires a verified JWT."""
    return permission in get_jwt().get("permissions", [])


def strip_unit_price_unless_permitted(rows: list[dict]) -> list[dict]:
    if not has_permission(PRICE_MANAGE_PERMISSION):
        for row in rows:
            row.pop("unit_price", None)
    return rows


def price_filters(q_on_hand, unit_price) -> list:
    """unit_price / gross value (on_hand * unit_price) range filters; empty without price:manage."""
    if not has_permission(PRICE_MANAGE_PERMISSION):
        return []
    gross = q_on_hand * unit_price
    bounds = [
        ("unit_price_min", unit_price, operator.ge),
        ("unit_price_max", unit_price, operator.le),
        ("gross_value_min", gross, operator.ge),
        ("gross_value_max", gross, operator.le),
    ]
    return [
        op(expr, value)
        for key, expr, op in bounds
        if (value := request.args.get(key, type=float)) is not None
    ]


def permission_required(*required_permissions):
    """Decorator that restricts access to users whose JWT contains at least one
    of the required permissions."""
    def decorator(fn):
        @wraps(fn)
        @jwt_required()
        def wrapper(*args, **kwargs):
            claims = get_jwt()
            user_permissions = claims.get("permissions", [])
            if not any(p in user_permissions for p in required_permissions):
                return jsonify({"error": "Forbidden: insufficient permissions"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator
