from flask import Blueprint, g, jsonify, request
from flask_jwt_extended import create_access_token
from sqlalchemy import select
from werkzeug.security import check_password_hash
from database.models import User, Role

auth_bp = Blueprint('auth', __name__)

# Dummy hash used to normalize timing when user is not found
_DUMMY_HASH = "pbkdf2:sha256:600000$x$x"


@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    db = g.db
    user = db.execute(select(User).where(User.email == email)).scalars().first()

    # Always run a hash check to prevent timing-based user enumeration
    if user is None:
        check_password_hash(_DUMMY_HASH, password)
        return jsonify({"error": "Invalid email or password"}), 401

    if not user.check_password(password):
        return jsonify({"error": "Invalid email or password"}), 401

    if not user.is_active:
        return jsonify({"error": "Account is deactivated"}), 403

    role_name = user.role.name if user.role else None
    permissions = (
        [p.name for p in user.role.permissions] if user.role else []
    )

    access_token = create_access_token(
        identity=str(user.id),
        additional_claims={
            "role": role_name,
            "permissions": permissions,
        }
    )

    return jsonify({
        "access_token": access_token,
        "email": user.email,
        "role": role_name,
        "permissions": permissions,
    }), 200

@auth_bp.route('/signup', methods=['POST'])
def signup():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    db = g.db
    email_check = db.execute(select(User).where(User.email == email)).scalars().first()

    # Always run a hash check to prevent timing-based user enumeration
    if email_check is not None:
        return jsonify({"error": "Email already exists"}), 409

    # Assign the first available role (Admin) by default
    admin_role = db.execute(select(Role).where(Role.name == "Admin")).scalar_one_or_none()

    user = User(email=email, role_id=admin_role.id if admin_role else None, is_active=True)
    user.set_password(password)
    db.add(user)
    db.commit()

    role_name = user.role.name if user.role else None

    return jsonify({
        "User": {
            "email": user.email,
            "role": role_name
        }
    }), 200
