"""
Input validation utilities for the API.

This module provides reusable validation functions to ensure data integrity
and security across the application.
"""
from typing import Any, Optional, Union
from datetime import datetime
import re

from app.utils.time import local_now


class ValidationError(Exception):
    """Custom exception for validation errors."""
    pass


def validate_positive_integer(value: Any, field_name: str = "value", allow_zero: bool = False) -> int:
    """
    Validate that a value is a positive integer.
    
    Args:
        value: The value to validate
        field_name: Name of the field (for error messages)
        allow_zero: Whether to allow zero as a valid value
    
    Returns:
        The validated integer value
    
    Raises:
        ValidationError: If validation fails
    """
    try:
        int_value = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field_name} must be an integer")
    
    min_value = 0 if allow_zero else 1
    if int_value < min_value:
        raise ValidationError(f"{field_name} must be >= {min_value}")
    
    return int_value


def validate_positive_number(value: Any, field_name: str = "value", allow_zero: bool = False) -> float:
    """
    Validate that a value is a positive number.
    
    Args:
        value: The value to validate
        field_name: Name of the field (for error messages)
        allow_zero: Whether to allow zero as a valid value
    
    Returns:
        The validated float value
    
    Raises:
        ValidationError: If validation fails
    """
    try:
        float_value = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field_name} must be a number")
    
    if allow_zero:
        if float_value < 0.0:
            raise ValidationError(f"{field_name} must be >= 0")
    else:
        if float_value <= 0.0:
            raise ValidationError(f"{field_name} must be > 0")
    
    return float_value


def validate_string(
    value: Any, 
    field_name: str = "value",
    min_length: int = 1,
    max_length: Optional[int] = None,
    allow_empty: bool = False,
    pattern: Optional[str] = None
) -> str:
    """
    Validate that a value is a string with optional length and pattern constraints.
    
    Args:
        value: The value to validate
        field_name: Name of the field (for error messages)
        min_length: Minimum string length
        max_length: Maximum string length (None for no limit)
        allow_empty: Whether to allow empty strings
        pattern: Optional regex pattern to match
    
    Returns:
        The validated string value
    
    Raises:
        ValidationError: If validation fails
    """
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    
    if not allow_empty and not value.strip():
        raise ValidationError(f"{field_name} cannot be empty")
    
    if len(value) < min_length:
        raise ValidationError(f"{field_name} must be at least {min_length} characters")
    
    if max_length is not None and len(value) > max_length:
        raise ValidationError(f"{field_name} must be at most {max_length} characters")
    
    if pattern and not re.match(pattern, value):
        raise ValidationError(f"{field_name} has invalid format")
    
    return value


def validate_date(
    value: Any,
    field_name: str = "value",
    date_format: str = "%Y-%m-%d",
    allow_future: bool = True,
    allow_past: bool = True
) -> datetime:
    """
    Validate and parse a date string.
    
    Args:
        value: The value to validate (string or datetime)
        field_name: Name of the field (for error messages)
        date_format: Expected date format string
        allow_future: Whether to allow future dates
        allow_past: Whether to allow past dates
    
    Returns:
        The parsed datetime object
    
    Raises:
        ValidationError: If validation fails
    """
    if isinstance(value, datetime):
        parsed_date = value
    elif isinstance(value, str):
        try:
            parsed_date = datetime.strptime(value, date_format)
        except ValueError:
            raise ValidationError(
                f"{field_name} must be in format {date_format}"
            )
    else:
        raise ValidationError(f"{field_name} must be a date string or datetime object")
    
    now = local_now()
    if not allow_future and parsed_date > now:
        raise ValidationError(f"{field_name} cannot be in the future")
    
    if not allow_past and parsed_date < now:
        raise ValidationError(f"{field_name} cannot be in the past")
    
    return parsed_date


def validate_enum(
    value: Any,
    allowed_values: list,
    field_name: str = "value",
    case_sensitive: bool = True
) -> str:
    """
    Validate that a value is one of the allowed values.
    
    Args:
        value: The value to validate
        allowed_values: List of allowed values
        field_name: Name of the field (for error messages)
        case_sensitive: Whether comparison should be case-sensitive
    
    Returns:
        The validated value
    
    Raises:
        ValidationError: If validation fails
    """
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    
    compare_value = value if case_sensitive else value.lower()
    compare_allowed = allowed_values if case_sensitive else [v.lower() for v in allowed_values]
    
    if compare_value not in compare_allowed:
        raise ValidationError(
            f"{field_name} must be one of: {', '.join(allowed_values)}"
        )
    
    return value


def validate_pagination(
    page: Any,
    limit: Any,
    max_limit: int = 100,
    default_page: int = 1,
    default_limit: int = 25
) -> tuple[int, int]:
    """
    Validate and normalize pagination parameters.
    
    Args:
        page: Page number
        limit: Items per page
        max_limit: Maximum allowed limit
        default_page: Default page number
        default_limit: Default items per page
    
    Returns:
        Tuple of (validated_page, validated_limit)
    
    Raises:
        ValidationError: If validation fails
    """
    try:
        page_num = int(page) if page is not None else default_page
        limit_num = int(limit) if limit is not None else default_limit
    except (TypeError, ValueError):
        raise ValidationError("page and limit must be integers")
    
    if page_num < 1:
        raise ValidationError("page must be >= 1")
    
    if limit_num < 1:
        raise ValidationError("limit must be >= 1")
    
    if limit_num > max_limit:
        raise ValidationError(f"limit cannot exceed {max_limit}")
    
    return page_num, limit_num


def sanitize_search_string(search: str, max_length: int = 100) -> str:
    """
    Sanitize a search string to prevent SQL injection.
    
    Args:
        search: The search string to sanitize
        max_length: Maximum allowed length
    
    Returns:
        Sanitized search string
    """
    if not isinstance(search, str):
        return ""
    
    # Remove any SQL-like characters
    search = search.strip()[:max_length]
    
    # SQLAlchemy's ilike should handle escaping, but we can add extra safety
    # Remove any % or _ that could be used for SQL LIKE wildcards
    # (let the application control wildcards explicitly)
    search = search.replace("%", "").replace("_", "")
    
    return search
