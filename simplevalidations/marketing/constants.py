"""Constants used by the marketing app."""

from __future__ import annotations

from enum import StrEnum

from django.db import models
from django.utils.translation import gettext_lazy as _

BLOCKLISTED_EMAIL_DOMAINS: set[str] = {
    "example.com",
    "example.org",
    "example.net",
    "mailinator.com",
    "tempmail.com",
    "trashmail.com",
    "discard.email",
    "guerrillamail.com",
    "10minutemail.com",
}


class ProspectOrigins(models.TextChoices):
    HERO = "hero", _("Homepage Hero")
    FOOTER = "footer", _("Footer")


class ProspectEmailStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    VERIFIED = "verified", _("Verified")
    INVALID = "invalid", _("Invalid")


class MarketingShareImage(StrEnum):
    """Paths to social sharing artwork for marketing pages."""

    DEFAULT = "marketing/images/robot_scene_platform_overview.png"
    SCHEMA_VALIDATION = "marketing/images/robot_scene_schema_validation.png"
    SIMULATION_VALIDATION = "marketing/images/robot_scene_simulations.png"
    CERTIFICATES = "marketing/images/robot_scene_certificates.png"
    BLOCKCHAIN = "marketing/images/robot_scene_blockchain.png"
    INTEGRATIONS = "marketing/images/robot_scene_integrations.png"
