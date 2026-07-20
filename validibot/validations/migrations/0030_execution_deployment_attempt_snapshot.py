"""Allow shared provider resources and snapshot managed attempt routing."""

from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    """Refine deployment cardinality and add provider-stage attempt facts."""

    dependencies = [
        ("validations", "0029_validator_execution_deployments"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="validatorexecutiondeployment",
            name="uq_execution_deployment_provider_resource",
        ),
        migrations.AddIndex(
            model_name="validatorexecutiondeployment",
            index=models.Index(
                fields=["provider_type", "provider_resource_name"],
                name="val_execdep_resource_idx",
            ),
        ),
        migrations.AddField(
            model_name="executionattempt",
            name="deployment_snapshot",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Secret-free immutable route and capability facts selected "
                    "with this attempt. Empty only for historical, local, and "
                    "self-hosted attempts."
                ),
            ),
        ),
        migrations.AddField(
            model_name="executionattempt",
            name="provider_accepted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="executionattempt",
            name="callback_received_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
