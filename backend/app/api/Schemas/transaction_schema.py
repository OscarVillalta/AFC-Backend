from marshmallow import Schema, fields, validate
from database.models import TransactionReason, TransactionState

class TransactionSchema(Schema):
    id = fields.Int(dump_only=True)
    product_id = fields.Int(allow_none=True, load_default=None)
    child_product_id = fields.Int(allow_none=True, load_default=None)
    order_id = fields.Int(load_default=None)
    order_item_id = fields.Int(load_default=None)
    warehouse_id = fields.Int(dump_only=True)
    quantity_delta = fields.Int(required=True)  # signed integer (+in, -out)

    reason = fields.Str(
        required=True,
        validate=validate.OneOf([e.value for e in TransactionReason])
    )

    state = fields.Str(
        load_default=TransactionState.PENDING.value,
        validate=validate.OneOf([e.value for e in TransactionState])
    )

    note = fields.Str(load_default=None)
    created_at = fields.DateTime(dump_only=True)
    last_updated_at = fields.DateTime(dump_only=True)
    ledger_sequence = fields.Int(dump_only=True, allow_none=True)

