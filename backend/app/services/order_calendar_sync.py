"""Helpers to keep order calendar rows and Google events in sync."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from app.api.error_handling import ExternalServiceError, InvalidInputError
from app.config import Config
from app.services import google_calendar_service as gcal
from database.models import CalendarSyncStatus, Order, OrderCalendarEvent, OrderType


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise InvalidInputError(f"Invalid datetime: {value}") from exc


def _sync_event_to_google(event: OrderCalendarEvent, order: Order) -> None:
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

    if event.google_event_id:
        gcal.update_event(calendar_id, event.google_event_id, body)
    else:
        event.google_event_id = gcal.insert_event(calendar_id, body)

    event.sync_status = CalendarSyncStatus.SYNCED.value
    event.last_synced_at = datetime.now(timezone.utc)
    event.last_error = None


def delete_order_calendar_event(
    db,
    order: Order,
    *,
    raise_on_error: bool = False,
) -> bool:
    event = db.execute(
        select(OrderCalendarEvent).where(OrderCalendarEvent.order_id == order.id)
    ).scalar_one_or_none()
    if not event:
        return False

    if event.google_event_id and Config.calendar_is_configured():
        try:
            gcal.delete_event(event.google_calendar_id, event.google_event_id)
        except ExternalServiceError:
            if raise_on_error:
                raise

    db.delete(event)
    return True


def sync_order_to_calendar(
    db,
    order: Order,
    *,
    overrides: Optional[dict[str, Any]] = None,
    raise_on_error: bool = False,
) -> tuple[Optional[OrderCalendarEvent], bool]:
    """Create or update the calendar event for an order.

    Returns (event, created). When an order should not have an event (void/no ETA),
    it removes any existing event and returns (None, False).
    """
    existing = db.execute(
        select(OrderCalendarEvent).where(OrderCalendarEvent.order_id == order.id)
    ).scalar_one_or_none()

    if order.type == OrderType.VOID.value or order.eta is None:
        if existing:
            delete_order_calendar_event(db, order, raise_on_error=raise_on_error)
        return None, False

    created = False
    if existing is None:
        existing = OrderCalendarEvent(
            order_id=order.id,
            google_calendar_id=Config.GOOGLE_CALENDAR_ID,
            title=gcal.build_default_title(order),
            description=gcal.build_default_description(order),
            starts_at=datetime.now(timezone.utc),
            ends_at=datetime.now(timezone.utc),
            all_day=True,
        )
        db.add(existing)
        created = True

    data = overrides or {}
    starts_at = _parse_iso_datetime(data.get("starts_at")) if "starts_at" in data else None
    ends_at = _parse_iso_datetime(data.get("ends_at")) if "ends_at" in data else None
    all_day = data.get("all_day") if "all_day" in data else None

    resolved_start, resolved_end, resolved_all_day = gcal.resolve_event_times(
        order,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
    )

    if "title" in data and data.get("title"):
        existing.title = str(data["title"]).strip()
    else:
        existing.title = gcal.build_default_title(order)

    if "description" in data:
        existing.description = data["description"]
    else:
        existing.description = gcal.build_default_description(order)

    existing.starts_at = resolved_start
    existing.ends_at = resolved_end
    existing.all_day = resolved_all_day
    existing.google_calendar_id = Config.GOOGLE_CALENDAR_ID

    if not Config.calendar_is_configured():
        message = "Calendar is not configured. Set CALENDAR_ID and GOOGLE_CALENDAR_CREDENTIALS_PATH."
        existing.sync_status = CalendarSyncStatus.ERROR.value
        existing.last_error = message
        if raise_on_error:
            raise ExternalServiceError("Google Calendar", message)
        return existing, created

    try:
        _sync_event_to_google(existing, order)
    except ExternalServiceError as exc:
        existing.sync_status = CalendarSyncStatus.ERROR.value
        existing.last_error = exc.message
        if raise_on_error:
            raise

    return existing, created
