from flask import Blueprint, g, jsonify, request
from flask_jwt_extended import create_access_token
from sqlalchemy import select
from werkzeug.security import check_password_hash
from database.models import User

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

    access_token = create_access_token(
        identity=str(user.id),
        additional_claims={"role": user.role}
    )

    return jsonify({
        "access_token": access_token,
        "email": user.email,
        "role": user.role
    }), 200
