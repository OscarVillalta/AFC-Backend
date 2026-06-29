"""Google Calendar API integration for order calendar events."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import Config
from app.api.error_handling import ExternalServiceError, InvalidInputError
from database.models import Order, OrderType


_calendar_service = None


def _get_calendar_service():
    global _calendar_service
    if _calendar_service is None:
        if not Config.calendar_is_configured():
            raise ExternalServiceError(
                "Google Calendar",
                "Calendar is not configured. Set CALENDAR_ID and credentials path.",
            )
        credentials = service_account.Credentials.from_service_account_file(
            str(Config.google_calendar_credentials_file()),
            scopes=Config.GOOGLE_CALENDAR_SCOPES,
        )
        _calendar_service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    return _calendar_service


def _party_name(order: Order) -> str:
    if order.type == OrderType.INCOMING.value:
        return order.supplier.name if order.supplier else "—"
    return order.customer.name if order.customer else "—"


def _order_detail_url(order_id: int) -> Optional[str]:
    base = Config.FRONTEND_ORDER_URL_BASE
    if not base:
        return None
    return f"{base}/{order_id}"


def _format_type_label(order_type: str) -> str:
    return (order_type or "order").replace("_", " ").title()


def color_id_for_order_type(order_type: str) -> Optional[str]:
    mapping = {
        OrderType.INCOMING.value: "6",
        OrderType.INSTALLATION.value: "9",
        OrderType.WILL_CALL.value: "3",
        OrderType.DELIVERY.value: "7",
        OrderType.SHIPMENT.value: "1",
    }
    return mapping.get(order_type)


def resolve_event_times(
    order: Order,
    *,
    starts_at: Optional[datetime] = None,
    ends_at: Optional[datetime] = None,
    all_day: Optional[bool] = None,
) -> tuple[datetime, datetime, bool]:
    """Resolve start/end/all_day using manual overrides or order ETA."""
    if starts_at is not None:
        resolved_all_day = all_day if all_day is not None else False
        if ends_at is None:
            if resolved_all_day:
                ends_at = starts_at + timedelta(days=1)
            else:
                ends_at = starts_at + timedelta(hours=1)
        return starts_at, ends_at, resolved_all_day

    if order.eta is not None:
        eta = order.eta
        if isinstance(eta, datetime):
            start = eta
        else:
            start = datetime.combine(eta, datetime.min.time(), tzinfo=timezone.utc)
        if all_day is None or all_day:
            end = start + timedelta(days=1)
            return start, end, True
        end = start + timedelta(hours=1)
        return start, end, False

    raise InvalidInputError(
        "Provide starts_at/end_at or set an order ETA before creating a calendar event."
    )


def build_default_title(order: Order) -> str:
    number = order.order_number or f"#{order.id}"
    return f"{number} · {_party_name(order)}"


def build_default_description(order: Order) -> str:
    lines = [
        f"Order: {order.order_number or order.id}",
        f"Type: {_format_type_label(order.type)}",
        f"Status: {order.status}",
    ]
    if order.external_order_number:
        lines.append(f"External #: {order.external_order_number}")
    if order.description:
        lines.append(f"Description: {order.description}")
    url = _order_detail_url(order.id)
    if url:
        lines.append(f"Link: {url}")
    return "\n".join(lines)


def _to_google_datetime(dt: datetime) -> dict[str, str]:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return {"dateTime": dt.isoformat(), "timeZone": "UTC"}


def _to_google_date(dt: datetime) -> dict[str, str]:
    if isinstance(dt, datetime):
        day = dt.date()
    else:
        day = dt
    return {"date": day.isoformat()}


def build_google_event_body(
    order: Order,
    *,
    title: str,
    description: Optional[str],
    starts_at: datetime,
    ends_at: datetime,
    all_day: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": title,
        "description": description or "",
        "extendedProperties": {
            "private": {
                "order_id": str(order.id),
                "warehouse_id": str(order.warehouse_id),
            }
        },
    }
    color_id = color_id_for_order_type(order.type)
    if color_id:
        body["colorId"] = color_id
    if all_day:
        start_day = starts_at.date() if isinstance(starts_at, datetime) else starts_at
        end_day = ends_at.date() if isinstance(ends_at, datetime) else ends_at
        body["start"] = {"date": start_day.isoformat()}
        body["end"] = {"date": end_day.isoformat()}
    else:
        body["start"] = _to_google_datetime(starts_at)
        body["end"] = _to_google_datetime(ends_at)

    url = _order_detail_url(order.id)
    if url:
        body["source"] = {"title": "AFC Inventory", "url": url}

    return body


def insert_event(calendar_id: str, body: dict[str, Any]) -> str:
    try:
        result = _get_calendar_service().events().insert(calendarId=calendar_id, body=body).execute()
        return result["id"]
    except HttpError as exc:
        raise ExternalServiceError("Google Calendar", _http_error_message(exc)) from exc


def update_event(calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
    try:
        _get_calendar_service().events().update(
            calendarId=calendar_id, eventId=event_id, body=body
        ).execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            raise ExternalServiceError("Google Calendar", "Event not found in Google Calendar.") from exc
        raise ExternalServiceError("Google Calendar", _http_error_message(exc)) from exc


def delete_event(calendar_id: str, event_id: str) -> None:
    try:
        _get_calendar_service().events().delete(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            return
        raise ExternalServiceError("Google Calendar", _http_error_message(exc)) from exc


def get_event(calendar_id: str, event_id: str) -> dict[str, Any]:
    try:
        return _get_calendar_service().events().get(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as exc:
        raise ExternalServiceError("Google Calendar", _http_error_message(exc)) from exc


def _http_error_message(exc: HttpError) -> str:
    try:
        content = exc.content.decode() if exc.content else str(exc)
    except Exception:
        content = str(exc)
    return content[:500]
