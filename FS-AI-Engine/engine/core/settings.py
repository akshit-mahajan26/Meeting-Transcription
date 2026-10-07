"""Settings module re-exporting centralized configuration for backward compatibility."""
from .config import Settings, get_settings, settings

__all__ = ["Settings", "get_settings", "settings"]
