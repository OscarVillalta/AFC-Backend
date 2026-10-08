"""Timezone helpers.

The database stores naive UTC timestamps. The business operates in Los Angeles,
so calendar days coming from the UI are interpreted as America/Los_Angeles days.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def utc_iso(dt: datetime | None) -> str | None:
    """Serialize a timestamp as ISO 8601 UTC with a trailing Z (naive values are UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def _parse_day(value: str | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value[:10])


def local_day_start_utc(value: str | date) -> datetime:
    """Naive UTC instant at which the given Los Angeles calendar day starts."""
    local_start = datetime.combine(_parse_day(value), time.min, tzinfo=LOCAL_TZ)
    return local_start.astimezone(timezone.utc).replace(tzinfo=None)


def local_day_end_utc(value: str | date) -> datetime:
    """Naive UTC instant at which the given Los Angeles calendar day ends (exclusive)."""
    return local_day_start_utc(_parse_day(value) + timedelta(days=1))


def is_date_only(value: str) -> bool:
    return len(value.strip()) == 10


def parse_utc_instant(value: str) -> datetime:
    """Parse an ISO timestamp into naive UTC (naive input is assumed UTC)."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def to_local(dt: datetime | None) -> datetime | None:
    """Convert a naive UTC timestamp to naive Los Angeles wall-clock time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).replace(tzinfo=None)


def local_now() -> datetime:
    """Current Los Angeles wall-clock time, naive."""
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)
