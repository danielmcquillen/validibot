from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Prospect",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True, verbose_name="created")),
                ("modified", models.DateTimeField(auto_now=True, verbose_name="modified")),
                ("email", models.EmailField(max_length=254, unique=True, verbose_name="Email")),
                (
                    "origin",
                    models.CharField(
                        choices=[("hero", "Homepage Hero"), ("footer", "Footer")],
                        default="hero",
                        max_length=20,
                        verbose_name="Signup origin",
                    ),
                ),
                (
                    "email_status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("verified", "Verified"), ("invalid", "Invalid")],
                        default="pending",
                        max_length=20,
                        verbose_name="Email status",
                    ),
                ),
                ("source", models.CharField(blank=True, max_length=100, verbose_name="Source")),
                ("referer", models.URLField(blank=True, max_length=500, verbose_name="HTTP referer")),
                ("user_agent", models.TextField(blank=True, verbose_name="User agent")),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True, verbose_name="IP address")),
                (
                    "welcome_sent_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Welcome email sent at"),
                ),
            ],
            options={
                "verbose_name": "Prospect",
                "verbose_name_plural": "Prospects",
                "ordering": ["-created"],
            },
        ),
    ]
