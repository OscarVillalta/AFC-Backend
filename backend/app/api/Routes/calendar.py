"""Calendar CRUD routes — sync orders to Google Calendar."""
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

from flask import Blueprint, g, jsonify, request
from sqlalchemy import select, func
from sqlalchemy.orm import joinedload

from app.config import Config
from app.api.tokens import permission_required
from app.api.validation import validate_pagination
from app.api.error_handling import (
    APIException,
    ResourceNotFoundError,
    DuplicateResourceError,
    InvalidInputError,
    ExternalServiceError,
    safe_commit,
    handle_database_error,
)
from app.services import google_calendar_service as gcal
from app.services.order_calendar_sync import sync_order_to_calendar
from database.models import Order, OrderCalendarEvent, CalendarSyncStatus

calendar_bp = Blueprint("calendar", __name__)


def _api_error_response(exc: APIException):
    return jsonify(exc.to_dict()), exc.status_code


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise InvalidInputError(f"Invalid datetime: {value}") from exc


def _ensure_calendar_configured() -> None:
    if not Config.calendar_is_configured():
        raise ExternalServiceError(
            "Google Calendar",
            "Calendar is not configured. Set CALENDAR_ID and GOOGLE_CALENDAR_CREDENTIALS_PATH.",
        )


def _get_order_in_warehouse(db, order_id: int) -> Order:
    order = db.execute(
        select(Order)
        .options(joinedload(Order.customer), joinedload(Order.supplier))
        .where(Order.id == order_id, Order.warehouse_id == g.active_warehouse_id)
    ).scalar_one_or_none()
    if not order:
        raise ResourceNotFoundError("Order", order_id)
    return order


def _event_to_dict(event: OrderCalendarEvent, include_order: bool = False) -> dict[str, Any]:
    data = event.to_dict()
    if include_order and event.order:
        data["order"] = {
            "id": event.order.id,
            "order_number": event.order.order_number,
            "type": event.order.type,
            "status": event.order.status,
            "external_order_number": event.order.external_order_number,
        }
    return data


def _sync_event_to_google(db, event: OrderCalendarEvent, order: Order) -> None:
    _ensure_calendar_configured()
    body = gcal.build_google_event_body(
        order,
        title=event.title,
        description=event.description,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        all_day=event.all_day,
    )
    calendar_id = event.google_calendar_id or Config.GOOGLE_CALENDAR_ID
    event.sync_status = CalendarSyncStatus.PENDING.value
    event.last_error = None

    try:
        if event.google_event_id:
            gcal.update_event(calendar_id, event.google_event_id, body)
        else:
            event.google_event_id = gcal.insert_event(calendar_id, body)
        event.sync_status = CalendarSyncStatus.SYNCED.value
        event.last_synced_at = datetime.now(timezone.utc)
        event.last_error = None
    except ExternalServiceError as exc:
        event.sync_status = CalendarSyncStatus.ERROR.value
        event.last_error = exc.message
        error = safe_commit(db)
        if error:
            handle_database_error(error)
        raise


def _apply_overrides_from_body(
    event: OrderCalendarEvent,
    order: Order,
    data: dict,
) -> None:
    if "title" in data and data["title"]:
        event.title = str(data["title"]).strip()
    if "description" in data:
        event.description = data["description"]

    starts_at = _parse_iso_datetime(data.get("starts_at")) if "starts_at" in data else None
    ends_at = _parse_iso_datetime(data.get("ends_at")) if "ends_at" in data else None
    all_day = data.get("all_day") if "all_day" in data else None

    if starts_at is not None or ends_at is not None or all_day is not None:
        base_start = starts_at or event.starts_at
        base_end = ends_at or event.ends_at
        base_all_day = all_day if all_day is not None else event.all_day
        resolved_start, resolved_end, resolved_all_day = gcal.resolve_event_times(
            order,
            starts_at=base_start,
            ends_at=base_end,
            all_day=base_all_day,
        )
        event.starts_at = resolved_start
        event.ends_at = resolved_end
        event.all_day = resolved_all_day


def _create_or_update_from_order(
    db,
    order: Order,
    data: Optional[dict] = None,
    existing: Optional[OrderCalendarEvent] = None,
) -> OrderCalendarEvent:
    data = data or {}
    starts_at = _parse_iso_datetime(data.get("starts_at"))
    ends_at = _parse_iso_datetime(data.get("ends_at"))
    all_day = data.get("all_day")

    resolved_start, resolved_end, resolved_all_day = gcal.resolve_event_times(
        order,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
    )

    if existing:
        event = existing
    else:
        existing_row = db.execute(
            select(OrderCalendarEvent).where(OrderCalendarEvent.order_id == order.id)
        ).scalar_one_or_none()
        if existing_row:
            raise DuplicateResourceError("Order calendar event", "order_id", order.id)
        event = OrderCalendarEvent(
            order_id=order.id,
            google_calendar_id=Config.GOOGLE_CALENDAR_ID,
            title=gcal.build_default_title(order),
            description=gcal.build_default_description(order),
            starts_at=resolved_start,
            ends_at=resolved_end,
            all_day=resolved_all_day,
        )
        db.add(event)

    if "title" in data and data.get("title"):
        event.title = str(data["title"]).strip()
    elif not existing:
        event.title = gcal.build_default_title(order)

    if "description" in data:
        event.description = data["description"]
    elif not existing or not event.description:
        event.description = gcal.build_default_description(order)

    event.starts_at = resolved_start
    event.ends_at = resolved_end
    event.all_day = resolved_all_day
    event.google_calendar_id = Config.GOOGLE_CALENDAR_ID

    _sync_event_to_google(db, event, order)
    return event


@calendar_bp.route("/calendar", methods=["GET"])
@permission_required("calendar:view")
def list_calendar_events() -> Tuple[Any, int]:
    try:
        db = g.db
        page, limit = validate_pagination(
            request.args.get("page"),
            request.args.get("limit"),
            max_limit=Config.MAX_PAGE_SIZE,
            default_page=1,
            default_limit=Config.DEFAULT_PAGE_SIZE,
        )
        offset = (page - 1) * limit

        start = _parse_iso_datetime(request.args.get("start"))
        end = _parse_iso_datetime(request.args.get("end"))
        order_id = request.args.get("order_id", type=int)

        query = (
            select(OrderCalendarEvent)
            .join(Order, OrderCalendarEvent.order_id == Order.id)
            .options(joinedload(OrderCalendarEvent.order))
            .where(Order.warehouse_id == g.active_warehouse_id)
        )

        if order_id:
            query = query.where(OrderCalendarEvent.order_id == order_id)
        if start:
            query = query.where(OrderCalendarEvent.ends_at >= start)
        if end:
            query = query.where(OrderCalendarEvent.starts_at <= end)

        count_query = (
            select(func.count())
            .select_from(OrderCalendarEvent)
            .join(Order, OrderCalendarEvent.order_id == Order.id)
            .where(Order.warehouse_id == g.active_warehouse_id)
        )
        if order_id:
            count_query = count_query.where(OrderCalendarEvent.order_id == order_id)
        if start:
            count_query = count_query.where(OrderCalendarEvent.ends_at >= start)
        if end:
            count_query = count_query.where(OrderCalendarEvent.starts_at <= end)
        total_count = db.execute(count_query).scalar() or 0

        events = db.execute(
            query.order_by(OrderCalendarEvent.starts_at.asc()).limit(limit).offset(offset)
        ).scalars().unique().all()

        return jsonify({
            "page": page,
            "limit": limit,
            "total": total_count,
            "results": [_event_to_dict(e, include_order=True) for e in events],
        }), 200
    except APIException as exc:
        return _api_error_response(exc)


@calendar_bp.route("/calendar/<int:event_id>", methods=["GET"])
@permission_required("calendar:view")
def get_calendar_event(event_id: int) -> Tuple[Any, int]:
    try:
        db = g.db
        event = db.execute(
            select(OrderCalendarEvent)
            .join(Order, OrderCalendarEvent.order_id == Order.id)
            .options(joinedload(OrderCalendarEvent.order))
            .where(
                OrderCalendarEvent.id == event_id,
                Order.warehouse_id == g.active_warehouse_id,
            )
        ).scalar_one_or_none()
        if not event:
            raise ResourceNotFoundError("Calendar event", event_id)
        return jsonify(_event_to_dict(event, include_order=True)), 200
    except APIException as exc:
        return _api_error_response(exc)


@calendar_bp.route("/calendar/order/<int:order_id>", methods=["GET"])
@permission_required("calendar:view")
def get_calendar_event_by_order(order_id: int) -> Tuple[Any, int]:
    try:
        db = g.db
        _get_order_in_warehouse(db, order_id)
        event = db.execute(
            select(OrderCalendarEvent)
            .options(joinedload(OrderCalendarEvent.order))
            .where(OrderCalendarEvent.order_id == order_id)
        ).scalar_one_or_none()
        if not event:
            raise ResourceNotFoundError("Calendar event for order", order_id)
        return jsonify(_event_to_dict(event, include_order=True)), 200
    except APIException as exc:
        return _api_error_response(exc)


@calendar_bp.route("/calendar", methods=["POST"])
@permission_required("calendar:manage")
def create_calendar_event() -> Tuple[Any, int]:
    try:
        db = g.db
        _ensure_calendar_configured()
        data = request.get_json() or {}

        order_id = data.get("order_id")
        if not order_id:
            raise InvalidInputError("order_id is required", field="order_id")

        order = _get_order_in_warehouse(db, int(order_id))
        try:
            event = _create_or_update_from_order(db, order, data)
        except ExternalServiceError as exc:
            error = safe_commit(db)
            if error:
                return handle_database_error(error)
            return _api_error_response(exc)

        error = safe_commit(db)
        if error:
            return handle_database_error(error)

        return jsonify(_event_to_dict(event, include_order=True)), 201
    except APIException as exc:
        return _api_error_response(exc)


@calendar_bp.route("/calendar/<int:event_id>", methods=["PATCH"])
@permission_required("calendar:manage")
def update_calendar_event(event_id: int) -> Tuple[Any, int]:
    try:
        db = g.db
        _ensure_calendar_configured()
        data = request.get_json() or {}

        event = db.execute(
            select(OrderCalendarEvent)
            .join(Order, OrderCalendarEvent.order_id == Order.id)
            .options(joinedload(OrderCalendarEvent.order))
            .where(
                OrderCalendarEvent.id == event_id,
                Order.warehouse_id == g.active_warehouse_id,
            )
        ).scalar_one_or_none()
        if not event:
            raise ResourceNotFoundError("Calendar event", event_id)

        _apply_overrides_from_body(event, event.order, data)

        try:
            _sync_event_to_google(db, event, event.order)
        except ExternalServiceError as exc:
            error = safe_commit(db)
            if error:
                return handle_database_error(error)
            return _api_error_response(exc)

        error = safe_commit(db)
        if error:
            return handle_database_error(error)

        return jsonify(_event_to_dict(event, include_order=True)), 200
    except APIException as exc:
        return _api_error_response(exc)


@calendar_bp.route("/calendar/<int:event_id>", methods=["DELETE"])
@permission_required("calendar:manage")
def delete_calendar_event(event_id: int) -> Tuple[Any, int]:
    try:
        db = g.db

        event = db.execute(
            select(OrderCalendarEvent)
            .join(Order, OrderCalendarEvent.order_id == Order.id)
            .where(
                OrderCalendarEvent.id == event_id,
                Order.warehouse_id == g.active_warehouse_id,
            )
        ).scalar_one_or_none()
        if not event:
            raise ResourceNotFoundError("Calendar event", event_id)

        if event.google_event_id and Config.calendar_is_configured():
            try:
                gcal.delete_event(event.google_calendar_id, event.google_event_id)
            except ExternalServiceError:
                pass

        db.delete(event)
        error = safe_commit(db)
        if error:
            return handle_database_error(error)

        return jsonify({"message": "Calendar event deleted."}), 200
    except APIException as exc:
        return _api_error_response(exc)


@calendar_bp.route("/calendar/order/<int:order_id>/sync", methods=["POST"])
@permission_required("calendar:manage")
def sync_calendar_event_for_order(order_id: int) -> Tuple[Any, int]:
    try:
        db = g.db
        _ensure_calendar_configured()
        data = request.get_json(silent=True) or {}

        order = _get_order_in_warehouse(db, order_id)
        event, created = sync_order_to_calendar(
            db,
            order,
            overrides=data,
            raise_on_error=True,
        )

        error = safe_commit(db)
        if error:
            return handle_database_error(error)

        if event is None:
            return jsonify({
                "message": "Order has no ETA or is void, so no calendar event was created."
            }), 200

        status_code = 201 if created else 200
        return jsonify(_event_to_dict(event, include_order=True)), status_code
    except APIException as exc:
        return _api_error_response(exc)
