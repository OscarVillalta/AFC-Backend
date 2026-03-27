from marshmallow import Schema, fields, validate


class UserSchema(Schema):
    id = fields.Int(dump_only=True)
    email = fields.Email(required=True)
    password = fields.Str(required=True, load_only=True, validate=validate.Length(min=8))
    role_id = fields.Int(required=True)
    is_active = fields.Bool(dump_only=True)
    role = fields.Method("get_role_name", dump_only=True)

    def get_role_name(self, obj):
        if obj.role:
            return obj.role.name
        return None
