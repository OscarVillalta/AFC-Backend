"""
Application configuration module.

This module centralizes all configuration values that were previously hard-coded
throughout the application. Values can be overridden via environment variables.
"""
import json
import os
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Config:
    """Application configuration class."""
    
    # QuickBooks Integration
    QB_AGENT_URL = os.getenv("QB_AGENT_URL", "http://127.0.0.1:5055")
    QB_API_KEY = os.getenv("QB_API_KEY", "")
    QB_REQUEST_TIMEOUT = int(os.getenv("QB_REQUEST_TIMEOUT", "30"))
    
    # QuickBooks Supplier
    # Auto-created supplier for products imported from QuickBooks
    QB_SUPPLIER_NAME = os.getenv("QB_SUPPLIER_NAME", "QuickBooks")
    
    # Pagination Defaults
    DEFAULT_PAGE_SIZE = int(os.getenv("DEFAULT_PAGE_SIZE", "25"))
    MAX_PAGE_SIZE = int(os.getenv("MAX_PAGE_SIZE", "100"))
    
    # Date Formats
    DATE_FORMAT = "%Y-%m-%d"
    DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
    
    # Transaction Defaults
    DEFAULT_TRANSACTION_DESCRIPTION = "Transaction"

    # Google Calendar (order sync)
    GOOGLE_CALENDAR_ID = os.getenv("CALENDAR_ID", "")
    GOOGLE_CALENDAR_CREDENTIALS_PATH = os.getenv(
        "GOOGLE_CALENDAR_CREDENTIALS_PATH",
        "packing-slips-tracker-calender-15e35c535e3a.json",
    )
    # Optional: full service-account JSON for hosts without a credentials file (e.g. Render).
    GOOGLE_CALENDAR_CREDENTIALS_JSON = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_JSON", "").strip()
    GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
    FRONTEND_ORDER_URL_BASE = os.getenv("FRONTEND_ORDER_URL_BASE", "").rstrip("/")

    @classmethod
    def google_calendar_credentials_file(cls) -> Path:
        path = Path(cls.GOOGLE_CALENDAR_CREDENTIALS_PATH)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    @classmethod
    def has_google_calendar_credentials(cls) -> bool:
        if cls.GOOGLE_CALENDAR_CREDENTIALS_JSON:
            try:
                json.loads(cls.GOOGLE_CALENDAR_CREDENTIALS_JSON)
                return True
            except json.JSONDecodeError:
                return False
        return cls.google_calendar_credentials_file().is_file()

    @classmethod
    def google_calendar_service_account_info(cls) -> Optional[dict[str, Any]]:
        if not cls.GOOGLE_CALENDAR_CREDENTIALS_JSON:
            return None
        return json.loads(cls.GOOGLE_CALENDAR_CREDENTIALS_JSON)

    @classmethod
    def calendar_is_configured(cls) -> bool:
        return bool(cls.GOOGLE_CALENDAR_ID) and cls.has_google_calendar_credentials()

    @classmethod
    def validate(cls):
        """Validate configuration values."""
        errors = []
        
        if cls.DEFAULT_PAGE_SIZE < 1 or cls.DEFAULT_PAGE_SIZE > cls.MAX_PAGE_SIZE:
            errors.append(f"DEFAULT_PAGE_SIZE must be between 1 and {cls.MAX_PAGE_SIZE}")
        
        if cls.QB_REQUEST_TIMEOUT < 1:
            errors.append("QB_REQUEST_TIMEOUT must be a positive integer")
        
        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")
        
        return True


# Validate configuration on module import
Config.validate()
