from marshmallow import Schema, fields


class WarehouseSchema(Schema):
    id = fields.Int(dump_only=True)
    name = fields.Str(required=True)
    address = fields.Str(allow_none=True, load_default=None)
    is_active = fields.Bool(load_default=True)
