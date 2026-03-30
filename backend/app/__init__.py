from flask import Flask, g, request
from datetime import timedelta
import os
from flask_jwt_extended import JWTManager
from database import SessionLocal
from backend.app.api.Routes.suppliers import supplier_bp
from backend.app.api.Routes.quantity import quantity_bp
from backend.app.api.Routes.transactions import transaction_bp
from backend.app.api.Routes.conversions import conversion_bp
from backend.app.api.Routes.air_filters import air_filter_bp
from backend.app.api.Routes.customers import customer_bp
from backend.app.api.Routes.products import product_bp
from backend.app.api.Routes.stock_items import stock_item_bp
from backend.app.api.Routes.orders import order_bp
from backend.app.api.Routes.order_items import order_item_bp
from backend.app.api.Routes.qb import qb_bp
from backend.app.api.Routes.child_products import child_product_bp
from backend.app.api.Routes.inventory_stats import inventory_stats_bp
from backend.app.api.Routes.tracker import tracker_bp
from backend.app.api.Routes.blocked_items import blocked_item_bp
from backend.app.api.Routes.media import media_bp
from backend.app.api.Routes.warehouses import warehouse_bp
from backend.app.api.Routes.transfers import transfer_bp
from backend.app.api.Routes.auth import auth_bp
from backend.app.api.Routes.users import user_bp
from backend.app.api.Routes.roles import role_bp
from flask_cors import CORS

# Default warehouse ID used when the X-Warehouse-Id header is absent
DEFAULT_WAREHOUSE_ID = 1


def create_app():
    app = Flask(__name__)
    CORS(app, resources={r"/*": {"origins": "*"}})

    # JWT configuration
    app.config["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY")
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(days=5)
    if not app.config["JWT_SECRET_KEY"]:
        raise RuntimeError("JWT_SECRET_KEY environment variable is not set.")
    
    JWTManager(app)

    #BluePrints
    app.register_blueprint(auth_bp, url_prefix='/api')
    app.register_blueprint(supplier_bp, url_prefix='/api')
    app.register_blueprint(air_filter_bp, url_prefix='/api')
    app.register_blueprint(quantity_bp, url_prefix='/api')
    app.register_blueprint(transaction_bp, url_prefix='/api')
    app.register_blueprint(conversion_bp, url_prefix="/api")
    app.register_blueprint(customer_bp, url_prefix="/api")
    app.register_blueprint(product_bp, url_prefix="/api")
    app.register_blueprint(stock_item_bp, url_prefix="/api")
    app.register_blueprint(order_bp, url_prefix="/api")
    app.register_blueprint(order_item_bp, url_prefix="/api")
    app.register_blueprint(qb_bp, url_prefix="/api")
    app.register_blueprint(child_product_bp, url_prefix="/api")
    app.register_blueprint(inventory_stats_bp, url_prefix="/api")
    app.register_blueprint(tracker_bp, url_prefix="/api")
    app.register_blueprint(blocked_item_bp, url_prefix="/api")
    app.register_blueprint(media_bp, url_prefix="/api")
    app.register_blueprint(warehouse_bp, url_prefix="/api")
    app.register_blueprint(transfer_bp, url_prefix="/api")
    app.register_blueprint(user_bp, url_prefix="/api")
    app.register_blueprint(role_bp, url_prefix="/api")


    #Db_session wrappers
    @app.before_request
    def start_db_session():
        g.db = SessionLocal()

    @app.before_request
    def set_active_warehouse():
        """Read X-Warehouse-Id header and store in g.active_warehouse_id."""
        raw = request.headers.get("X-Warehouse-Id")
        if raw is not None:
            try:
                g.active_warehouse_id = int(raw)
            except (ValueError, TypeError):
                from flask import jsonify
                return jsonify({"error": "Invalid X-Warehouse-Id header: must be an integer"}), 400
        else:
            g.active_warehouse_id = DEFAULT_WAREHOUSE_ID

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        db = getattr(g, "db", None)
        if db is not None:
            db.rollback() 
            db.close()

    return app
