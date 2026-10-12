"""
Shared helpers for building query filters used across search endpoints.
"""

from flask import request
from sqlalchemy import func, select, String

from app.api.tokens import PRICE_MANAGE_PERMISSION, has_permission

VALID_COMPARE_MODES = ("eq", "lte", "gte")
PRICE_SORT_KEYS = ("unit_price", "gross_value")


def _parse_stock_param(name):
    """Return (value, compare_mode) for a stock-level query parameter.

    Query-string contract
    ---------------------
    * ``<name>``         – integer value to compare against
    * ``<name>_compare`` – one of ``eq``, ``lte``, ``gte`` (default ``lte``)
    """
    value = request.args.get(name, type=int)
    compare = request.args.get(f"{name}_compare", default="gte").lower()
    if compare not in VALID_COMPARE_MODES:
        compare = "gte"
    return value, compare


def stock_level_filter(quantity_field, value, compare):
    """Build a SQLAlchemy filter expression for a stock-level field.

    Parameters
    ----------
    quantity_field : SQLAlchemy column / hybrid property expression
    value : int
    compare : str – ``"eq"`` | ``"lte"`` | ``"gte"``
    """
    if compare == "eq":
        return quantity_field == value
    elif compare == "lte":
        return quantity_field <= value
    else:  # default: gte
        return quantity_field >= value


def apply_sort(query, sortable):
    """Order a search query by the ``sort_by`` / ``sort_dir`` query parameters.

    ``sortable`` maps each accepted ``sort_by`` key to a result column name, or
    ``"gross_value"`` to ``on_hand * unit_price``. The query is wrapped in a
    subquery because PostgreSQL requires ``DISTINCT ON`` expressions to lead
    the ORDER BY of the same SELECT.
    """
    sort_by = request.args.get("sort_by")
    if sort_by not in sortable:
        return query
    if sort_by in PRICE_SORT_KEYS and not has_permission(PRICE_MANAGE_PERMISSION):
        return query

    sub = query.subquery()
    column_name = sortable[sort_by]
    if column_name == "gross_value":
        expr = sub.c.on_hand * sub.c.unit_price
    else:
        expr = sub.c[column_name]
        if isinstance(expr.type, String):
            expr = func.lower(expr)

    descending = request.args.get("sort_dir", "asc").lower() == "desc"
    expr = expr.desc() if descending else expr.asc()
    return select(sub).order_by(expr.nulls_last(), sub.c.id)
