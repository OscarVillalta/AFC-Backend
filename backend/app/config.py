"""
Application configuration module.

This module centralizes all configuration values that were previously hard-coded
throughout the application. Values can be overridden via environment variables.
"""
import os


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
