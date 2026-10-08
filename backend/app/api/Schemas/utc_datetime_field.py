from marshmallow import fields

from app.utils.time import utc_iso


class UTCDateTime(fields.DateTime):
    """DateTime field that dumps naive UTC values as ISO 8601 with a trailing Z."""

    def _serialize(self, value, attr, obj, **kwargs):
        return utc_iso(value)
