from django.db import migrations
from django.db import models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteSettings",
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
                ("created", models.DateTimeField(auto_now_add=True)),
                ("modified", models.DateTimeField(auto_now=True)),
                (
                    "slug",
                    models.SlugField(
                        default="default",
                        help_text="Identifier for this settings record. Only 'default' is used.",
                        max_length=100,
                        unique=True,
                    ),
                ),
                (
                    "data",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="JSON payload containing namespaced site configuration.",
                    ),
                ),
            ],
            options={
                "verbose_name": "Site settings",
                "verbose_name_plural": "Site settings",
            },
        ),
    ]
