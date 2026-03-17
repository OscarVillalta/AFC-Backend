from marshmallow import Schema, fields, validate, pre_load
from database.models import OrderStatus, OrderType

class OrderSchema(Schema):
    id = fields.Int(dump_only=True)

    # AFC internal number (server-generated)
    order_number = fields.Str(dump_only=True)

    # External system (QuickBooks)
    external_order_number = fields.Str(
        allow_none=True,
        validate=validate.Length(min=1)
    )

    customer_id = fields.Int(allow_none=True)
    supplier_id = fields.Int(allow_none=True)
    warehouse_id = fields.Int(dump_only=True)

    description = fields.Str(allow_none=True)
    eta = fields.DateTime(allow_none=True)

    @pre_load
    def normalize_eta(self, data, **kwargs):
        eta = data.get("eta")

        if isinstance(eta, str) and len(eta) == 10:  # YYYY-MM-DD
            data["eta"] = f"{eta}T00:00:00"

        return data

    type = fields.Str(
        required=True,
        validate=validate.OneOf([e.value for e in OrderType])
    )

    status = fields.Str(
        dump_only=True,
        validate=validate.OneOf([e.value for e in OrderStatus])
    )

    created_at = fields.DateTime(dump_only=True)
    completed_at = fields.DateTime(allow_none=True)
