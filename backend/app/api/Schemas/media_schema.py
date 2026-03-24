from marshmallow import Schema, fields, validate


class MediaSchema(Schema):
    id = fields.Int(dump_only=True)
    part_number = fields.Str(required=True, validate=validate.Length(min=1))
    description = fields.Str(load_default=None, allow_none=True)
    length = fields.Float(load_default=None, allow_none=True)
    width = fields.Float(load_default=None, allow_none=True)
    unit_of_measure = fields.Str(load_default=None, allow_none=True)
    supplier_id = fields.Int(required=True)
    category_id = fields.Int(required=True)


class MediaCategorySchema(Schema):
    id = fields.Int(dump_only=True)
    name = fields.Str(required=True, validate=validate.Length(min=1))
