from flask import Blueprint, g, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt
from sqlalchemy import select, func, or_, case, and_
from sqlalchemy.exc import IntegrityError, DatabaseError
from database.models import Order, OrderTracker, OrderHistory, OrderTrackerStage, Department, OutgoingOrderType, Customer, Supplier, OrderType, OUTGOING_TYPES, UserRole
from datetime import datetime, timezone
from typing import Tuple, Any

from app.api.error_handling import (
    handle_database_error,
    safe_commit,
    ResourceNotFoundError,
)

# All order types that participate in the packing-slip tracker
# (outgoing types + incoming / purchase orders)
TRACKER_TYPES = OUTGOING_TYPES | {OrderType.INCOMING.value}

# Maps each non-admin UserRole to the Departments they are allowed to update.
ROLE_DEPARTMENT_MAP = {
    UserRole.SALES.value: [Department.SALES.value],
    UserRole.WAREHOUSE.value: [Department.LOGISTICS.value, Department.DELIVERY_DEPT.value],
    UserRole.SERVICE.value: [Department.SERVICE.value],
}

# SQLAlchemy CASE expression: total stages expected for each order type
# Must mirror the frontend step-path definitions (INSTALLATION_STEPS, WILL_CALL_STEPS, PURCHASE_ORDER_STEPS)
_total_steps_expr = case(
    (Order.type == OrderType.INSTALLATION.value, 6),
    (Order.type.in_([OrderType.WILL_CALL.value, OrderType.DELIVERY.value, OrderType.SHIPMENT.value]), 4),
    else_=3,  # incoming / purchase order (and legacy "outgoing")
)

tracker_bp = Blueprint("tracker", __name__)


@tracker_bp.route("/orders/<int:order_id>/tracker", methods=["GET"])
@jwt_required()
def get_order_tracker(order_id: int) -> Tuple[Any, int]:
    """Return the current tracking state and full history for an order."""
    db = g.db

    order = db.get(Order, order_id)
    if not order:
        raise ResourceNotFoundError("Order", order_id)

    tracker = db.execute(
        select(OrderTracker).where(OrderTracker.order_id == order_id)
    ).scalar_one_or_none()

    history_rows = db.execute(
        select(OrderHistory)
        .where(OrderHistory.order_id == order_id)
        .order_by(OrderHistory.completed_at.asc())
    ).scalars().all()

    stage_rows = db.execute(
        select(OrderTrackerStage)
        .where(OrderTrackerStage.order_id == order_id)
        .order_by(OrderTrackerStage.stage_index.asc())
    ).scalars().all()

    return jsonify({
        "order": order.to_dict(),
        "tracker": tracker.to_dict() if tracker else None,
        "history": [h.to_dict() for h in history_rows],
        "stages": [s.to_dict() for s in stage_rows],
    }), 200


@tracker_bp.route("/orders/<int:order_id>/tracker", methods=["POST"])
@jwt_required()
def create_order_tracker(order_id: int) -> Tuple[Any, int]:
    """Initialize tracking for an order (sets current_department and step_index)."""
    db = g.db
    data = request.get_json() or {}

    order = db.get(Order, order_id)
    if not order:
        raise ResourceNotFoundError("Order", order_id)

    existing = db.execute(
        select(OrderTracker).where(OrderTracker.order_id == order_id)
    ).scalar_one_or_none()
    if existing:
        return jsonify({"error": "Tracker already exists for this order."}), 409

    current_department = data.get("current_department")
    if not current_department or current_department not in [d.value for d in Department]:
        return jsonify({
            "error": "current_department must be one of: " + ", ".join(d.value for d in Department)
        }), 400

    tracker = OrderTracker(
        order_id=order_id,
        current_department=current_department,
        step_index=data.get("step_index", 0),
        warehouse_id=g.active_warehouse_id,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(tracker)

    error = safe_commit(db)
    if error:
        return handle_database_error(error)

    return jsonify(tracker.to_dict()), 201


@tracker_bp.route("/orders/<int:order_id>/tracker", methods=["PATCH"])
@jwt_required()
def update_order_tracker(order_id: int) -> Tuple[Any, int]:
    """Advance the tracker to a new department/step or set backordered flag."""
    db = g.db
    data = request.get_json() or {}

    user_role = get_jwt().get("role")
    if user_role != UserRole.ADMIN.value:
        target_department = data.get("current_department")
        if target_department and target_department not in ROLE_DEPARTMENT_MAP.get(user_role, []):
            return jsonify({"error": "Forbidden: Your role does not have permission to update steps for this department."}), 403

    order = db.get(Order, order_id)
    if not order:
        raise ResourceNotFoundError("Order", order_id)

    tracker = db.execute(
        select(OrderTracker).where(OrderTracker.order_id == order_id)
    ).scalar_one_or_none()
    if not tracker:
        return jsonify({"error": "Tracker not found for this order."}), 404

    current_department = data.get("current_department")
    if current_department is not None:
        if current_department not in [d.value for d in Department]:
            return jsonify({
                "error": "current_department must be one of: " + ", ".join(d.value for d in Department)
            }), 400
        tracker.current_department = current_department

    if "step_index" in data:
        tracker.step_index = data["step_index"]

    if "is_backordered" in data:
        is_backordered = data["is_backordered"]
        if not isinstance(is_backordered, bool):
            return jsonify({"error": "is_backordered must be a boolean."}), 400
        tracker.is_backordered = is_backordered

    tracker.updated_at = datetime.now(timezone.utc)

    error = safe_commit(db)
    if error:
        return handle_database_error(error)

    return jsonify(tracker.to_dict()), 200


@tracker_bp.route("/orders/<int:order_id>/history", methods=["POST"])
@jwt_required()
def add_order_history(order_id: int) -> Tuple[Any, int]:
    """Append a history entry (department transition + action) for an order."""
    db = g.db
    data = request.get_json() or {}

    user_role = get_jwt().get("role")
    if user_role != UserRole.ADMIN.value:
        allowed = ROLE_DEPARTMENT_MAP.get(user_role, [])
        to_dept = data.get("to_department")
        from_dept = data.get("from_department")
        if (to_dept and to_dept not in allowed) or (from_dept and from_dept not in allowed):
            return jsonify({"error": "Forbidden: Your role does not have permission to update steps for this department."}), 403

    order = db.get(Order, order_id)
    if not order:
        raise ResourceNotFoundError("Order", order_id)

    department_values = [d.value for d in Department]

    to_department = data.get("to_department")
    if not to_department or to_department not in department_values:
        return jsonify({
            "error": "to_department must be one of: " + ", ".join(department_values)
        }), 400

    from_department = data.get("from_department")
    if from_department and from_department not in department_values:
        return jsonify({
            "error": "from_department must be one of: " + ", ".join(department_values)
        }), 400

    action_taken = data.get("action_taken", "").strip()
    if not action_taken:
        return jsonify({"error": "action_taken is required."}), 400

    performed_by = data.get("performed_by", "").strip()
    if not performed_by:
        return jsonify({"error": "performed_by is required."}), 400

    entry = OrderHistory(
        order_id=order_id,
        from_department=from_department,
        to_department=to_department,
        action_taken=action_taken,
        performed_by=performed_by,
        completed_at=datetime.now(timezone.utc),
        comments=data.get("comments"),
    )
    db.add(entry)

    error = safe_commit(db)
    if error:
        return handle_database_error(error)

    return jsonify(entry.to_dict()), 201


@tracker_bp.route("/orders/<int:order_id>/tracker/stages/<int:stage_index>", methods=["PATCH"])
@jwt_required()
def toggle_tracker_stage(order_id: int, stage_index: int) -> Tuple[Any, int]:
    """Toggle the completion state of a specific tracker stage for an order."""
    db = g.db
    data = request.get_json() or {}

    user_role = get_jwt().get("role")
    if user_role != UserRole.ADMIN.value:
        target_department = data.get("department")
        if target_department and target_department not in ROLE_DEPARTMENT_MAP.get(user_role, []):
            return jsonify({"error": "Forbidden: Your role does not have permission to update steps for this department."}), 403

    order = db.get(Order, order_id)
    if not order:
        raise ResourceNotFoundError("Order", order_id)

    is_completed = data.get("is_completed")
    if is_completed is None or not isinstance(is_completed, bool):
        return jsonify({"error": "is_completed (boolean) is required."}), 400

    completed_by = data.get("completed_by", "").strip() or None

    stage = db.execute(
        select(OrderTrackerStage).where(
            OrderTrackerStage.order_id == order_id,
            OrderTrackerStage.stage_index == stage_index,
        )
    ).scalar_one_or_none()

    if stage is None:
        stage = OrderTrackerStage(
            order_id=order_id,
            stage_index=stage_index,
            is_completed=is_completed,
            completed_by=completed_by if is_completed else None,
            completed_at=datetime.now(timezone.utc) if is_completed else None,
        )
        db.add(stage)
    else:
        stage.is_completed = is_completed
        stage.completed_by = completed_by if is_completed else None
        stage.completed_at = datetime.now(timezone.utc) if is_completed else None

    # Clear the backordered flag whenever a stage is toggled
    tracker = db.execute(
        select(OrderTracker).where(OrderTracker.order_id == order_id)
    ).scalar_one_or_none()
    if tracker and tracker.is_backordered:
        tracker.is_backordered = False
        tracker.updated_at = datetime.now(timezone.utc)

    error = safe_commit(db)
    if error:
        return handle_database_error(error)

    return jsonify(stage.to_dict()), 200


@tracker_bp.route("/packing-slips", methods=["GET"])
@jwt_required()
def get_packing_slips() -> Tuple[Any, int]:
    """Return all tracker-eligible orders (outgoing + purchase/incoming) with their tracker and history info."""
    db = g.db

    page = request.args.get("page", default=1, type=int)
    limit = request.args.get("limit", default=25, type=int)
    search = request.args.get("search", "").strip()
    tracker_status = request.args.get("tracker_status", "").strip()  # "Not Started"|"In Progress"|"Completed"|"Backordered"
    order_type = request.args.get("order_type", "").strip()
    offset = (page - 1) * limit

    # Correlated sub-query: count of completed stages for each order row
    _completed_stages_subq = (
        select(func.count(OrderTrackerStage.id))
        .where(
            OrderTrackerStage.order_id == Order.id,
            OrderTrackerStage.is_completed == True,  # noqa: E712
        )
        .correlate(Order)
        .scalar_subquery()
    )

    # Status conditions (mirror the frontend toPackingSlipRow logic)
    _backordered_cond = OrderTracker.is_backordered == True  # noqa: E712
    _not_started_cond = and_(OrderTracker.id.is_(None), _completed_stages_subq == 0)
    _completed_cond = and_(
        _completed_stages_subq >= _total_steps_expr,
        or_(OrderTracker.id.is_(None), OrderTracker.is_backordered == False),  # noqa: E712
    )
    _in_progress_cond = and_(
        or_(OrderTracker.id.is_not(None), _completed_stages_subq > 0),
        _completed_stages_subq < _total_steps_expr,
        or_(OrderTracker.id.is_(None), OrderTracker.is_backordered == False),  # noqa: E712
    )

    # Base query: all tracker-eligible orders joined with customer, supplier and tracker
    base_query = (
        select(Order)
        .outerjoin(Customer, Order.customer_id == Customer.id)
        .outerjoin(Supplier, Order.supplier_id == Supplier.id)
        .outerjoin(OrderTracker, OrderTracker.order_id == Order.id)
        .where(Order.type.in_(TRACKER_TYPES))
        .where(Order.warehouse_id == g.active_warehouse_id)
    )

    if search:
        base_query = base_query.where(
            or_(
                Order.order_number.ilike(f"%{search}%"),
                Order.external_order_number.ilike(f"%{search}%"),
                Customer.name.ilike(f"%{search}%"),
                Supplier.name.ilike(f"%{search}%"),
            )
        )

    if order_type:
        base_query = base_query.where(Order.type == order_type)

    # Tracker status filter using stage-completion counts
    if tracker_status == "Not Started":
        base_query = base_query.where(_not_started_cond)
    elif tracker_status == "In Progress":
        base_query = base_query.where(_in_progress_cond)
    elif tracker_status == "Completed":
        base_query = base_query.where(_completed_cond)
    elif tracker_status == "Backordered":
        base_query = base_query.where(_backordered_cond)

    # Efficient total count using the filtered query as a subquery
    subq = base_query.subquery()
    total = db.execute(
        select(func.count()).select_from(subq)
    ).scalar()

    orders = db.execute(
        base_query.order_by(Order.created_at.desc()).limit(limit).offset(offset)
    ).scalars().all()

    # Per-status counts for the tab badges (based on search, ignoring tracker_status)
    _search_filter = (
        or_(
            Order.order_number.ilike(f"%{search}%"),
            Order.external_order_number.ilike(f"%{search}%"),
            Customer.name.ilike(f"%{search}%"),
            Supplier.name.ilike(f"%{search}%"),
        )
        if search
        else None
    )
    counts_query = (
        select(
            func.count(case((_backordered_cond, 1))).label("backordered"),
            func.count(case((_not_started_cond, 1))).label("not_started"),
            func.count(case((_in_progress_cond, 1))).label("in_progress"),
            func.count(case((_completed_cond, 1))).label("completed"),
        )
        .select_from(Order)
        .outerjoin(Customer, Order.customer_id == Customer.id)
        .outerjoin(Supplier, Order.supplier_id == Supplier.id)
        .outerjoin(OrderTracker, OrderTracker.order_id == Order.id)
        .where(Order.type.in_(TRACKER_TYPES))
        .where(Order.warehouse_id == g.active_warehouse_id)
    )
    if _search_filter is not None:
        counts_query = counts_query.where(_search_filter)
    if order_type:
        counts_query = counts_query.where(Order.type == order_type)
    counts_row = db.execute(counts_query).one()
    status_counts = {
        "Not Started": counts_row.not_started or 0,
        "In Progress": counts_row.in_progress or 0,
        "Completed": counts_row.completed or 0,
        "Backordered": counts_row.backordered or 0,
    }

    results = []
    for order in orders:
        tracker = order.tracker
        history = sorted(order.history, key=lambda h: h.completed_at)
        stages = sorted(order.stages, key=lambda s: s.stage_index)
        results.append({
            "id": order.id,
            "order_number": order.order_number,
            "external_order_number": order.external_order_number,
            "order_type": order.type,
            "status": order.status,
            "description": order.description,
            "customer_name": order.customer.name if order.customer else None,
            "supplier_name": order.supplier.name if order.supplier else None,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "completed_at": order.completed_at.isoformat() if order.completed_at else None,
            "eta": order.eta.strftime("%Y-%m-%d") if order.eta else None,
            "is_paid": order.is_paid,
            "is_invoiced": order.is_invoiced,
            "tracker": tracker.to_dict() if tracker else None,
            "history": [h.to_dict() for h in history],
            "stages": [s.to_dict() for s in stages],
        })

    return jsonify({
        "page": page,
        "limit": limit,
        "total": total,
        "status_counts": status_counts,
        "results": results,
    }), 200
