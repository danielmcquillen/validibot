"""
Helpers for loading and validating platform-wide settings.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel
from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from validibot.core.models import SiteSettings

logger = logging.getLogger(__name__)


class MetadataPolicyError(Exception):
    """Raised when submission metadata violates the configured policy."""


class APISubmissionSettings(BaseModel):
    """
    Settings that govern how workflow start requests are processed.
    """

    metadata_key_value_only: bool = Field(
        default=False,
        description=(
            "When true, metadata values must be scalars (no nested lists/dicts)."
        ),
    )
    metadata_max_bytes: int = Field(
        default=4096,
        ge=0,
        description=(
            "Maximum size (in bytes) of stored metadata. Zero disables the limit."
        ),
    )

    def enforce_metadata_policy(self, metadata: dict[str, Any]) -> None:
        """
        Validate metadata against the configured rules.
        """
        if self.metadata_key_value_only:
            for key, value in metadata.items():
                if isinstance(value, (dict, list)):
                    raise MetadataPolicyError(
                        f"Metadata value for '{key}' must be a scalar when "
                        "key/value enforcement is enabled.",
                    )
        if self.metadata_max_bytes > 0:
            size = len(json.dumps(metadata).encode("utf-8"))
            if size > self.metadata_max_bytes:
                raise MetadataPolicyError(
                    "Metadata is too large for this workflow start request.",
                )


class SiteSettingsModel(BaseModel):
    """
    Strongly typed overlay for the settings JSON document.
    """

    api_submission: APISubmissionSettings = APISubmissionSettings()


def _normalize_site_settings(instance: SiteSettings, model: SiteSettingsModel) -> None:
    normalized = model.model_dump()
    if instance.data != normalized:
        instance.data = normalized
        instance.save(update_fields=["data", "modified"])


def get_site_settings() -> SiteSettingsModel:
    """
    Fetch the singleton SiteSettings row and return a typed view of its data.
    """
    obj, _ = SiteSettings.objects.get_or_create(
        slug=SiteSettings.DEFAULT_SLUG,
        defaults={"data": {}},
    )
    raw_data = obj.data or {}
    try:
        model = SiteSettingsModel(**raw_data)
    except PydanticValidationError:
        logger.warning(
            "Invalid site settings JSON detected; falling back to defaults.",
            exc_info=True,
        )
        model = SiteSettingsModel()
    _normalize_site_settings(obj, model)
    return model
