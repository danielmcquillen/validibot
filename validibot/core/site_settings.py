"""
Helpers for loading platform-wide settings.

The ``SiteSettings`` model lives in ``core.models``.  This module provides
the ``get_site_settings()`` convenience function and re-exports
``MetadataPolicyError`` so existing import paths keep working.
"""

from __future__ import annotations

import logging

from validibot.core.models import MetadataPolicyError
from validibot.core.models import SiteSettings

logger = logging.getLogger(__name__)

# Re-export so callers that import from here still work.
__all__ = ["MetadataPolicyError", "get_site_settings"]


def get_site_settings() -> SiteSettings:
    """Fetch (or create) the singleton SiteSettings row."""
    obj, _ = SiteSettings.objects.get_or_create(
        slug=SiteSettings.DEFAULT_SLUG,
    )
    return obj
