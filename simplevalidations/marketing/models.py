from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from simplevalidations.marketing.constants import ProspectEmailStatus, ProspectOrigins


class Prospect(TimeStampedModel):
    email = models.EmailField(_("Email"), unique=True)
    origin = models.CharField(
        _("Signup origin"),
        max_length=20,
        choices=ProspectOrigins.choices,
        default=ProspectOrigins.HERO,
    )
    email_status = models.CharField(
        _("Email status"),
        max_length=20,
        choices=ProspectEmailStatus.choices,
        default=ProspectEmailStatus.PENDING,
    )
    source = models.CharField(_("Source"), max_length=100, blank=True)
    referer = models.URLField(_("HTTP referer"), max_length=500, blank=True)
    user_agent = models.TextField(_("User agent"), blank=True)
    ip_address = models.GenericIPAddressField(_("IP address"), blank=True, null=True)
    welcome_sent_at = models.DateTimeField(
        _("Welcome email sent at"), blank=True, null=True
    )

    class Meta:
        ordering = ["-created"]
        verbose_name = _("Prospect")
        verbose_name_plural = _("Prospects")

    def __str__(self) -> str:
        return self.email
