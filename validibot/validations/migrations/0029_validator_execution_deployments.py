"""Add immutable managed-provider deployment identity to execution attempts."""

import uuid

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import model_utils.fields
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    """Create deployment records and make attempt provider naming neutral."""

    dependencies = [
        ("validations", "0028_executionattempt_input_evidence_snapshot"),
    ]

    operations = [
        migrations.RenameField(
            model_name="executionattempt",
            old_name="provider_job_name",
            new_name="provider_resource_name",
        ),
        migrations.RemoveConstraint(
            model_name="executionattempt",
            name="uq_attempt_provider_execution",
        ),
        migrations.AddConstraint(
            model_name="executionattempt",
            constraint=models.UniqueConstraint(
                condition=~models.Q(provider_execution_id=""),
                fields=(
                    "runner_type",
                    "provider_resource_name",
                    "provider_execution_id",
                ),
                name="uq_attempt_provider_execution",
            ),
        ),
        migrations.CreateModel(
            name="ValidatorExecutionDeployment",
            fields=[
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "provider_type",
                    models.CharField(
                        choices=[("GCP", "Google Cloud Platform")],
                        max_length=16,
                    ),
                ),
                (
                    "deployment_kind",
                    models.CharField(
                        choices=[
                            ("CLOUD_RUN_JOB", "Cloud Run Job"),
                            ("CLOUD_RUN_SERVICE", "Cloud Run Service"),
                        ],
                        max_length=32,
                    ),
                ),
                ("display_name", models.CharField(max_length=160)),
                ("deployment_revision", models.CharField(max_length=128)),
                ("provider_configuration", models.JSONField(default=dict)),
                ("provider_resource_name", models.CharField(max_length=512)),
                (
                    "route",
                    models.CharField(blank=True, default="", max_length=2048),
                ),
                (
                    "authentication_audience",
                    models.CharField(blank=True, default="", max_length=2048),
                ),
                ("backend_release_identity", models.CharField(max_length=128)),
                ("backend_image_ref", models.CharField(max_length=512)),
                ("backend_image_digest", models.CharField(max_length=71)),
                ("expected_runtime_identity", models.CharField(max_length=254)),
                ("declared_capabilities", models.JSONField(default=dict)),
                (
                    "verified_capabilities",
                    models.JSONField(blank=True, default=dict),
                ),
                (
                    "readiness_state",
                    models.CharField(
                        choices=[
                            ("DRAFT", "Draft"),
                            ("VERIFYING", "Verifying"),
                            ("READY", "Ready"),
                            ("FAILED", "Verification failed"),
                            ("RETIRED", "Retired"),
                        ],
                        default="DRAFT",
                        max_length=16,
                    ),
                ),
                (
                    "last_verification_succeeded",
                    models.BooleanField(blank=True, null=True),
                ),
                (
                    "last_verification_details",
                    models.JSONField(blank=True, default=dict),
                ),
                ("last_verified_at", models.DateTimeField(blank=True, null=True)),
                (
                    "routing_role",
                    models.CharField(
                        choices=[
                            ("INACTIVE", "Inactive"),
                            ("PRIMARY", "Primary"),
                            ("LONG_RUNNING", "Long-running compatibility"),
                        ],
                        default="INACTIVE",
                        max_length=16,
                    ),
                ),
                ("emergency_blocked", models.BooleanField(default=False)),
                (
                    "emergency_block_reason",
                    models.CharField(blank=True, default="", max_length=500),
                ),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                (
                    "maximum_execution_seconds",
                    models.PositiveIntegerField(
                        validators=[django.core.validators.MinValueValidator(1)]
                    ),
                ),
                (
                    "request_timeout_seconds",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                (
                    "dispatch_timeout_seconds",
                    models.PositiveIntegerField(
                        validators=[django.core.validators.MinValueValidator(1)]
                    ),
                ),
                ("minimum_instances", models.PositiveIntegerField(default=0)),
                (
                    "maximum_instances",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                (
                    "concurrency",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(1)],
                    ),
                ),
                (
                    "validator",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="execution_deployments",
                        to="validations.validator",
                    ),
                ),
            ],
            options={
                "ordering": [
                    "validator_id",
                    "provider_type",
                    "deployment_kind",
                    "created",
                ],
                "indexes": [
                    models.Index(
                        fields=["validator", "readiness_state", "routing_role"],
                        name="val_execdep_route_idx",
                    ),
                    models.Index(
                        fields=["provider_type", "deployment_kind"],
                        name="val_execdep_provider_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=(
                            "validator",
                            "provider_type",
                            "deployment_kind",
                            "deployment_revision",
                        ),
                        name="uq_validator_execution_deployment_revision",
                    ),
                    models.UniqueConstraint(
                        fields=("provider_type", "provider_resource_name"),
                        name="uq_execution_deployment_provider_resource",
                    ),
                    models.UniqueConstraint(
                        condition=~models.Q(routing_role="INACTIVE"),
                        fields=("validator", "routing_role"),
                        name="uq_validator_execution_routing_slot",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            readiness_state__in=[
                                "DRAFT",
                                "VERIFYING",
                                "READY",
                                "FAILED",
                                "RETIRED",
                            ]
                        ),
                        name="ck_execution_deployment_readiness_known",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            routing_role__in=[
                                "INACTIVE",
                                "PRIMARY",
                                "LONG_RUNNING",
                            ]
                        ),
                        name="ck_execution_deployment_routing_role_known",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="executionattempt",
            name="deployment",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Exact managed-provider deployment selected before dispatch. "
                    "Empty only for historical, local, and self-hosted attempts."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="execution_attempts",
                to="validations.validatorexecutiondeployment",
            ),
        ),
    ]
