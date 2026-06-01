from marshmallow import Schema, fields

class OrderItemSchema(Schema):
    id = fields.Int(dump_only=True)
    order_id = fields.Int(required=True)
    product_id = fields.Int(allow_none=True)
    child_product_id = fields.Int(allow_none=True, load_default=None)
    type = fields.Str(load_default="Product_Item")
    quantity_ordered = fields.Int(load_default=0)
    quantity_fulfilled = fields.Int(dump_only=True)
    note = fields.Str(allow_none=True)
    position = fields.Int(load_default=None)
