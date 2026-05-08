"""Add ``allow_guest_access`` and ``allow_guest_invites`` flags to SiteSettings.

Two operator-controlled booleans that toggle guest-account behaviour
without a code change. Both default to ``True`` — existing deployments
upgrade transparently, keeping the prior behaviour where guests can log
in and invites can be sent and accepted. Operators flip them to
``False`` for incident response or when winding down a feature; the
auth backend and invite views check these values at runtime.
"""

from __future__ import annotations

from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_sitesettings_proper_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="allow_guest_access",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "When False, users classified as GUEST cannot log in. "
                    "Existing guest accounts are not deleted — just denied "
                    "access while the flag is False."
                ),
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="allow_guest_invites",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "When False, no user (other than superusers) can create "
                    "OR accept guest invites, regardless of role. Two-sided "
                    "gate so pending invites cannot be redeemed during a "
                    "disable window."
                ),
            ),
        ),
    ]
