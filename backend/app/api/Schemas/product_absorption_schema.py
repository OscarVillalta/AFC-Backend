from marshmallow import Schema, fields


class ProductAbsorptionSchema(Schema):
    parent_product_id = fields.Int(required=True)
