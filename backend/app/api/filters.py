"""
Shared helpers for building query filters used across search endpoints.
"""

from flask import request

VALID_COMPARE_MODES = ("eq", "lte", "gte")


def _parse_stock_param(name):
    """Return (value, compare_mode) for a stock-level query parameter.

    Query-string contract
    ---------------------
    * ``<name>``         – integer value to compare against
    * ``<name>_compare`` – one of ``eq``, ``lte``, ``gte`` (default ``lte``)
    """
    value = request.args.get(name, type=int)
    compare = request.args.get(f"{name}_compare", default="lte").lower()
    if compare not in VALID_COMPARE_MODES:
        compare = "lte"
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
    elif compare == "gte":
        return quantity_field >= value
    else:  # default: lte
        return quantity_field <= value
