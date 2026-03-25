from functools import wraps
from flask import jsonify
from flask_jwt_extended import jwt_required, get_jwt


def role_required(*allowed_roles):
    """Decorator that restricts access to users whose JWT contains an allowed role."""
    def decorator(fn):
        @wraps(fn)
        @jwt_required()
        def wrapper(*args, **kwargs):
            claims = get_jwt()
            user_role = claims.get("role")
            if user_role not in allowed_roles:
                return jsonify({"error": "Forbidden: insufficient role"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator