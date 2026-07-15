from marshmallow import Schema, fields, validate


class ProductMigrationSchema(Schema):
    target_type = fields.Str(
        required=True,
        validate=validate.OneOf(["air_filters", "stock_items", "media"]),
    )
    target_category_id = fields.Int(required=True)
    overrides = fields.Dict(keys=fields.Str(), values=fields.Raw(), load_default=None)
    child_overrides = fields.Dict(keys=fields.Str(), values=fields.Dict(), load_default=None)
