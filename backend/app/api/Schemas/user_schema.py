from marshmallow import Schema, fields, validate
from database.models import UserRole


_VALID_ROLES = [r.value for r in UserRole]


class UserSchema(Schema):
    id = fields.Int(dump_only=True)
    email = fields.Email(required=True)
    password = fields.Str(required=True, load_only=True, validate=validate.Length(min=8))
    role = fields.Str(required=True, validate=validate.OneOf(_VALID_ROLES))
    is_active = fields.Bool(dump_only=True)
