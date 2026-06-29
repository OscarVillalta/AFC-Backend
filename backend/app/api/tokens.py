from functools import wraps
from flask import jsonify
from flask_jwt_extended import jwt_required, get_jwt


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
