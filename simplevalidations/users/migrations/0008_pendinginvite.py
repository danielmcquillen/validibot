from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0007_add_analytics_viewer_and_rename_results_viewer"),
    ]

    operations = [
        migrations.CreateModel(
            name="PendingInvite",
            fields=[
                ("created", models.DateTimeField(auto_now_add=True, verbose_name="created")),
                ("modified", models.DateTimeField(auto_now=True, verbose_name="modified")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("invitee_email", models.EmailField(blank=True, max_length=254, null=True)),
                ("roles", models.JSONField(default=list)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("ACCEPTED", "Accepted"),
                            ("DECLINED", "Declined"),
                            ("CANCELED", "Canceled"),
                            ("EXPIRED", "Expired"),
                        ],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("expires_at", models.DateTimeField()),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False)),
                (
                    "invitee_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="received_invites",
                        to="users.user",
                    ),
                ),
                (
                    "inviter",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sent_invites",
                        to="users.user",
                    ),
                ),
                (
                    "org",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pending_invites",
                        to="users.organization",
                    ),
                ),
            ],
            options={
                "ordering": ["-created"],
            },
        ),
    ]
