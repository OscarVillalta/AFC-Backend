from flask import Blueprint, g, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt, get_jwt_identity
from sqlalchemy import select, func, or_, case, and_
from sqlalchemy.exc import IntegrityError, DatabaseError
from database.models import Order, OrderTracker, OrderHistory, OrderTrackerStage, Department, OutgoingOrderType, Customer, Supplier, OrderType, OrderStatus, OUTGOING_TYPES, User
from datetime import datetime, timezone
from typing import Tuple, Any

from app.api.error_handling import (
    handle_database_error,
    safe_commit,
    ResourceNotFoundError,
)

# All order types that participate in the packing-slip tracker
# (outgoing types + incoming / purchase orders)
TRACKER_TYPES = OUTGOING_TYPES | {OrderType.INCOMING.value, OrderType.VOID.value}

# Maps each Department value to the permission required to update it.
DEPARTMENT_PERMISSION_MAP = {
    Department.SALES.value: "tracker:update_sales",
    Department.SERVICE.value: "tracker:update_service",
    Department.LOGISTICS.value: "tracker:update_logistics",
    Department.WAREHOUSE.value: "tracker:update_delivery",
}

_TRACKER_DEPARTMENT_FILTER_VALUES = {
    Department.SALES.value,
    Department.LOGISTICS.value,
    Department.WAREHOUSE.value,
    Department.SERVICE.value,
    "COMPLETED",
}

TRACKER_UPDATE_ANY = "tracker:update_any"


def _parse_list_arg(name: str) -> list[str]:
    """Parse repeated query params and/or comma-separated values into a deduped list."""
    seen: set[str] = set()
    values: list[str] = []
    for raw in request.args.getlist(name):
        for part in raw.split(","):
            value = part.strip()
            if value and value not in seen:
                seen.add(value)
                values.append(value)
    return values


def _parse_party_filter() -> tuple[tuple[list[int], list[int]], str]:
    """Parse party=customer:{id} and party=supplier:{id} list args."""
    customer_ids: list[int] = []
    supplier_ids: list[int] = []
    seen: set[str] = set()

    for raw in _parse_list_arg("party"):
        if raw in seen:
            continue
        seen.add(raw)

        if raw.startswith("customer:"):
            try:
                customer_ids.append(int(raw.split(":", 1)[1]))
            except (ValueError, IndexError):
                return None, "Invalid party filter value."
        elif raw.startswith("supplier:"):
            try:
                supplier_ids.append(int(raw.split(":", 1)[1]))
            except (ValueError, IndexError):
                return None, "Invalid party filter value."
        else:
            return None, "Invalid party filter value."

    return (customer_ids, supplier_ids), ""


def _party_filter_cond(customer_ids: list[int], supplier_ids: list[int]):
    party_conds = []
    if customer_ids:
        party_conds.append(Order.customer_id.in_(customer_ids))
    if supplier_ids:
        party_conds.append(Order.supplier_id.in_(supplier_ids))
    return or_(*party_conds) if party_conds else None


def _order_number_search_cond(search: str):
    return or_(
        Order.order_number.ilike(f"%{search}%"),
        Order.external_order_number.ilike(f"%{search}%"),
    )


def _department_filter_cond(department: str, completed_stages_subq):
    """Orders currently positioned at the given tracker department (in progress only)."""
    not_started_sales = and_(
        OrderTracker.id.is_(None),
        completed_stages_subq == 0,
        Order.type != OrderType.INCOMING.value,
    )
    not_started_logistics = and_(
        OrderTracker.id.is_(None),
        completed_stages_subq == 0,
        Order.type == OrderType.INCOMING.value,
    )

    if department == Department.SALES.value:
        dept_cond = or_(OrderTracker.current_department == department, not_started_sales)
    elif department == Department.LOGISTICS.value:
        dept_cond = or_(OrderTracker.current_department == department, not_started_logistics)
    else:
        dept_cond = OrderTracker.current_department == department

    # Fully completed orders keep the last step's department on the tracker row;
    # exclude them so they only match the dedicated COMPLETED filter.
    return and_(dept_cond, completed_stages_subq < _total_steps_expr)

# Step paths per order type — must mirror frontend trackerSteps.ts
_INSTALLATION_STEPS = [
    Department.SALES.value,
    Department.LOGISTICS.value,
    Department.WAREHOUSE.value,
    Department.SERVICE.value,
    Department.SALES.value,
    Department.LOGISTICS.value,
]
_WILL_CALL_STEPS = [
    Department.SALES.value,
    Department.LOGISTICS.value,
    Department.WAREHOUSE.value,
    Department.LOGISTICS.value,
]
_PURCHASE_ORDER_STEPS = [
    Department.LOGISTICS.value,
    Department.WAREHOUSE.value,
    Department.LOGISTICS.value,
]


def _steps_for_order_type(order_type: str) -> list[str]:
    """Return ordered department values for the tracker path of an order type."""
    t = (order_type or "").lower()
    if t == OrderType.INSTALLATION.value:
        return _INSTALLATION_STEPS
    if t == OrderType.INCOMING.value:
        return _PURCHASE_ORDER_STEPS
    return _WILL_CALL_STEPS


def _first_incomplete_index(stages: list[OrderTrackerStage], total_steps: int) -> int:
    stage_map = {s.stage_index: s for s in stages}
    for i in range(total_steps):
        if not stage_map.get(i) or not stage_map[i].is_completed:
            return i
    return -1


def _last_completed_index(stages: list[OrderTrackerStage], total_steps: int) -> int:
    stage_map = {s.stage_index: s for s in stages}
    for i in range(total_steps - 1, -1, -1):
        if stage_map.get(i) and stage_map[i].is_completed:
            return i
    return -1


def _check_department_permission(user_permissions, department):
    """Return an error response if the user lacks permission for the given department, or None if allowed."""
    if TRACKER_UPDATE_ANY in user_permissions:
        return None
    required_perm = DEPARTMENT_PERMISSION_MAP.get(department)
    if required_perm and required_perm not in user_permissions:
        return jsonify({"error": "Forbidden: You do not have permission to update this department."}), 403
    return None


def _resolve_completed_by(data: dict) -> str | None:
    """Prefer authenticated user email; fall back to client-supplied completed_by."""
    user_id = get_jwt_identity()
    if user_id:
        db = g.db
        user = db.get(User, int(user_id))
        if user and user.email:
            return user.email
    completed_by = (data.get("completed_by") or "").strip()
    return completed_by or None


# SQLAlchemy CASE expression: total stages expected for each order type
# Must mirror the frontend step-path definitions (INSTALLATION_STEPS, WILL_CALL_STEPS, PURCHASE_ORDER_STEPS)
_total_steps_expr = case(
    (Order.type == OrderType.INSTALLATION.value, 6),
    (Order.type.in_([OrderType.WILL_CALL.value, OrderType.DELIVERY.value, OrderType.SHIPMENT.value]), 4),
    else_=3,  # incoming / purchase order
)


def _parse_date(value: str | None) -> datetime | None:
    """Parse an ISO date string, returning None if the value is empty/None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


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
    """Advance the tracker to a new department/step, set backordered flag, or mark invoiced/paid."""
    db = g.db
    data = request.get_json() or {}

    user_permissions = get_jwt().get("permissions", [])

    # Permission check for department updates
    target_department = data.get("current_department")
    if target_department:
        denied = _check_department_permission(user_permissions, target_department)
        if denied:
            return denied

    # Permission check for marking as invoiced
    if "is_invoiced" in data and "orders:mark_invoiced" not in user_permissions:
        return jsonify({"error": "Forbidden: You do not have permission to mark orders as invoiced."}), 403

    # Permission check for marking as paid
    if "is_paid" in data and "orders:mark_paid" not in user_permissions:
        return jsonify({"error": "Forbidden: You do not have permission to mark orders as paid."}), 403

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

    if "is_invoiced" in data:
        is_invoiced = data["is_invoiced"]
        if not isinstance(is_invoiced, bool):
            return jsonify({"error": "is_invoiced must be a boolean."}), 400
        order.is_invoiced = is_invoiced

    if "is_paid" in data:
        is_paid = data["is_paid"]
        if not isinstance(is_paid, bool):
            return jsonify({"error": "is_paid must be a boolean."}), 400
        order.is_paid = is_paid

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

    user_permissions = get_jwt().get("permissions", [])
    to_dept = data.get("to_department")
    from_dept = data.get("from_department")
    if to_dept:
        denied = _check_department_permission(user_permissions, to_dept)
        if denied:
            return denied
    if from_dept:
        denied = _check_department_permission(user_permissions, from_dept)
        if denied:
            return denied

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

    user_permissions = get_jwt().get("permissions", [])

    order = db.get(Order, order_id)
    if not order:
        raise ResourceNotFoundError("Order", order_id)

    steps = _steps_for_order_type(order.type)
    if stage_index < 0 or stage_index >= len(steps):
        return jsonify({"error": "Invalid stage_index for this order type."}), 400

    target_department = steps[stage_index]
    denied = _check_department_permission(user_permissions, target_department)
    if denied:
        return denied

    is_completed = data.get("is_completed")
    if is_completed is None or not isinstance(is_completed, bool):
        return jsonify({"error": "is_completed (boolean) is required."}), 400

    existing_stages = db.execute(
        select(OrderTrackerStage).where(OrderTrackerStage.order_id == order_id)
    ).scalars().all()

    stage = db.execute(
        select(OrderTrackerStage).where(
            OrderTrackerStage.order_id == order_id,
            OrderTrackerStage.stage_index == stage_index,
        )
    ).scalar_one_or_none()

    if stage is not None and stage.is_completed == is_completed:
        return jsonify({"error": "Stage is already in the requested state."}), 409

    completed_by = _resolve_completed_by(data) if is_completed else None

    if stage is None:
        stage = OrderTrackerStage(
            order_id=order_id,
            stage_index=stage_index,
            is_completed=is_completed,
            completed_by=completed_by,
            completed_at=datetime.now(timezone.utc) if is_completed else None,
        )
        db.add(stage)
    else:
        stage.is_completed = is_completed
        stage.completed_by = completed_by
        stage.completed_at = datetime.now(timezone.utc) if is_completed else None

    tracker = db.execute(
        select(OrderTracker).where(OrderTracker.order_id == order_id)
    ).scalar_one_or_none()

    if tracker:
        if tracker.is_backordered:
            tracker.is_backordered = False
        # Refresh current position after stage change
        updated_stages = list(existing_stages)
        stage_in_list = next((s for s in updated_stages if s.stage_index == stage_index), None)
        if stage_in_list:
            stage_in_list.is_completed = is_completed
        elif stage not in updated_stages:
            updated_stages.append(stage)
        new_first_incomplete = _first_incomplete_index(updated_stages, len(steps))
        if new_first_incomplete >= 0:
            tracker.current_department = steps[new_first_incomplete]
            tracker.step_index = new_first_incomplete
        else:
            tracker.current_department = steps[-1]
            tracker.step_index = len(steps) - 1
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
    tracker_status = request.args.get("tracker_status", "").strip()
    stock_state = request.args.get("stock_state", "").strip()
    tracker_departments = _parse_list_arg("tracker_department")
    order_types = _parse_list_arg("order_type")
    offset = (page - 1) * limit

    # Date Created filters (Order.created_at)
    start_date_raw = request.args.get("start_date", type=str)
    end_date_raw = request.args.get("end_date", type=str)
    before_date_raw = request.args.get("before_date", type=str)
    after_date_raw = request.args.get("after_date", type=str)

    # Last Updated filters (OrderTracker.updated_at)
    last_updated_start_raw = request.args.get("last_updated_start", type=str)
    last_updated_end_raw = request.args.get("last_updated_end", type=str)
    last_updated_before_raw = request.args.get("last_updated_before", type=str)
    last_updated_after_raw = request.args.get("last_updated_after", type=str)

    # Parse all date params (invalid values become None and are silently ignored)
    start_date = _parse_date(start_date_raw)
    end_date = _parse_date(end_date_raw)
    before_date = _parse_date(before_date_raw)
    after_date = _parse_date(after_date_raw)
    last_updated_start = _parse_date(last_updated_start_raw)
    last_updated_end = _parse_date(last_updated_end_raw)
    last_updated_before = _parse_date(last_updated_before_raw)
    last_updated_after = _parse_date(last_updated_after_raw)

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
        base_query = base_query.where(_order_number_search_cond(search))

    party_result, party_error = _parse_party_filter()
    if party_error:
        return jsonify({"error": party_error}), 400
    customer_ids, supplier_ids = party_result
    if customer_ids or supplier_ids:
        party_cond = _party_filter_cond(customer_ids, supplier_ids)
        if party_cond is not None:
            base_query = base_query.where(party_cond)

    if order_types:
        invalid_types = [t for t in order_types if t not in TRACKER_TYPES]
        if invalid_types:
            return jsonify({"error": f"Invalid order_type: {invalid_types[0]}"}), 400
        base_query = base_query.where(Order.type.in_(order_types))

    # Date Created filters (Order.created_at)
    if start_date and end_date:
        base_query = base_query.where(Order.created_at >= start_date)
        base_query = base_query.where(Order.created_at <= end_date)
    elif before_date:
        base_query = base_query.where(Order.created_at <= before_date)
    elif after_date:
        base_query = base_query.where(Order.created_at >= after_date)

    # Last Updated filters (OrderTracker.updated_at)
    if last_updated_start and last_updated_end:
        base_query = base_query.where(OrderTracker.updated_at.is_not(None))
        base_query = base_query.where(OrderTracker.updated_at >= last_updated_start)
        base_query = base_query.where(OrderTracker.updated_at <= last_updated_end)
    elif last_updated_before:
        base_query = base_query.where(OrderTracker.updated_at.is_not(None))
        base_query = base_query.where(OrderTracker.updated_at <= last_updated_before)
    elif last_updated_after:
        base_query = base_query.where(OrderTracker.updated_at.is_not(None))
        base_query = base_query.where(OrderTracker.updated_at >= last_updated_after)

    # Tracker status filter using stage-completion counts
    if tracker_status == "Not Started":
        base_query = base_query.where(_not_started_cond)
    elif tracker_status == "In Progress":
        base_query = base_query.where(_in_progress_cond)
    elif tracker_status == "Completed":
        base_query = base_query.where(_completed_cond)
    elif tracker_status == "Backordered":
        base_query = base_query.where(_backordered_cond)

    # Stock state filter (mirrors frontend: Completed → Delivered, else Reserved)
    if stock_state == "Delivered":
        base_query = base_query.where(Order.status == OrderStatus.COMPLETED.value)
    elif stock_state == "Reserved":
        base_query = base_query.where(Order.status != OrderStatus.COMPLETED.value)

    if tracker_departments:
        invalid_departments = [
            d for d in tracker_departments if d not in _TRACKER_DEPARTMENT_FILTER_VALUES
        ]
        if invalid_departments:
            return jsonify({"error": "Invalid tracker_department."}), 400
        department_conds = []
        for department in tracker_departments:
            if department == "COMPLETED":
                department_conds.append(_completed_cond)
            else:
                department_conds.append(
                    _department_filter_cond(department, _completed_stages_subq)
                )
        base_query = base_query.where(or_(*department_conds))

    # Efficient total count using the filtered query as a subquery
    subq = base_query.subquery()
    total = db.execute(
        select(func.count()).select_from(subq)
    ).scalar()

    orders = db.execute(
        base_query.order_by(Order.order_number.desc()).limit(limit).offset(offset)
    ).scalars().all()

    # Per-status counts for the tab badges (based on search/filters, ignoring tracker_status)
    _search_filter = _order_number_search_cond(search) if search else None
    _party_filter = None
    if customer_ids or supplier_ids:
        _party_filter = _party_filter_cond(customer_ids, supplier_ids)
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
    if _party_filter is not None:
        counts_query = counts_query.where(_party_filter)
    if order_types:
        counts_query = counts_query.where(Order.type.in_(order_types))

    # Apply same date filters to counts query
    if start_date and end_date:
        counts_query = counts_query.where(Order.created_at >= start_date)
        counts_query = counts_query.where(Order.created_at <= end_date)
    elif before_date:
        counts_query = counts_query.where(Order.created_at <= before_date)
    elif after_date:
        counts_query = counts_query.where(Order.created_at >= after_date)

    if last_updated_start and last_updated_end:
        counts_query = counts_query.where(OrderTracker.updated_at.is_not(None))
        counts_query = counts_query.where(OrderTracker.updated_at >= last_updated_start)
        counts_query = counts_query.where(OrderTracker.updated_at <= last_updated_end)
    elif last_updated_before:
        counts_query = counts_query.where(OrderTracker.updated_at.is_not(None))
        counts_query = counts_query.where(OrderTracker.updated_at <= last_updated_before)
    elif last_updated_after:
        counts_query = counts_query.where(OrderTracker.updated_at.is_not(None))
        counts_query = counts_query.where(OrderTracker.updated_at >= last_updated_after)

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
