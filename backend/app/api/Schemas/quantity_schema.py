from marshmallow import Schema, fields

class QuantitySchema(Schema):
    id = fields.Int(dump_only=True)
    product_id = fields.Int(required=True)
    warehouse_id = fields.Int(dump_only=True)
    on_hand = fields.Int(load_default=0)
    reserved = fields.Int(load_default=0)
    ordered = fields.Int(load_default=0)
    location = fields.Int(load_default=1)
    