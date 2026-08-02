"""Add the durable public verification-key registry.

The table contains public JWKs only. Google Cloud KMS or a local PEM remains
the private-key authority; retaining these rows lets old credentials verify
after the configured signing key changes.
"""

import model_utils.fields
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_sitesettings_guest_kill_switches"),
    ]

    operations = [
        migrations.CreateModel(
            name="CredentialVerificationKey",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(verbose_name="created"),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(verbose_name="modified"),
                ),
                (
                    "kid",
                    models.CharField(
                        help_text=(
                            "Stable key identifier published in credential JOSE "
                            "headers."
                        ),
                        max_length=255,
                        unique=True,
                    ),
                ),
                (
                    "jwk",
                    models.JSONField(
                        help_text="Public JSON Web Key used for verification."
                    ),
                ),
                (
                    "provider_reference",
                    models.CharField(
                        blank=True,
                        help_text=(
                            "Optional provider reference, such as a Google Cloud "
                            "KMS key version."
                        ),
                        max_length=1024,
                    ),
                ),
            ],
            options={
                "verbose_name": "Credential verification key",
                "verbose_name_plural": "Credential verification keys",
                "ordering": ["created", "kid"],
            },
        ),
    ]
