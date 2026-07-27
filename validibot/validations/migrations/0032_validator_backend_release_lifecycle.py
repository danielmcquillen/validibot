"""Add independent backend-release identity and pair lifecycle facts."""

import django.utils.translation
from django.db import migrations
from django.db import models

MANAGED_BACKENDS = {
    "ENERGYPLUS": "energyplus",
    "FMU": "fmu",
    "SHACL": "shacl",
    "SCHEMATRON": "schematron",
    "PORTFOLIO_MANAGER": "portfolio_manager",
}
RUNTIME_CONTRACT = "validibot-execution-v1"


def backfill_validator_backend_contracts(apps, schema_editor):
    """Record managed backend compatibility without fabricating release facts."""
    validator_model = apps.get_model("validations", "Validator")
    deployment_model = apps.get_model(
        "validations",
        "ValidatorExecutionDeployment",
    )
    for validation_type, backend_slug in MANAGED_BACKENDS.items():
        validator_model.objects.filter(validation_type=validation_type).update(
            execution_backend_slug=backend_slug,
            execution_runtime_contract=RUNTIME_CONTRACT,
        )
        deployment_model.objects.filter(
            validator__validation_type=validation_type
        ).update(backend_slug=backend_slug)


class Migration(migrations.Migration):
    """Extend existing records while preserving unknown legacy release facts."""

    dependencies = [
        ("validations", "0031_alter_ruleset_ruleset_type_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="validator",
            name="execution_backend_slug",
            field=models.CharField(
                blank=True,
                default="",
                help_text=django.utils.translation.gettext_lazy(
                    "Managed backend slug used by this semantic Validator, such as "
                    "energyplus. Empty for validators without a managed backend."
                ),
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="validator",
            name="execution_runtime_contract",
            field=models.CharField(
                blank=True,
                default="",
                help_text=django.utils.translation.gettext_lazy(
                    "Runtime contract required from managed deployments. Empty for "
                    "validators without a managed backend."
                ),
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="validatorexecutiondeployment",
            name="backend_slug",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="validatorexecutiondeployment",
            name="source_release_tag",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="validatorexecutiondeployment",
            name="release_record_sha256",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="validatorexecutiondeployment",
            name="provider_spec_sha256",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="validatorexecutiondeployment",
            name="execution_config_sha256",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="validatorexecutiondeployment",
            name="accepted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="validatorexecutiondeployment",
            name="deactivated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="validatorexecutiondeployment",
            name="deactivation_cause",
            field=models.CharField(
                blank=True,
                choices=[
                    (
                        "SUPERSEDED_BY_ACCEPTED_RELEASE",
                        "Superseded by an accepted release",
                    ),
                    ("RELEASE_ROLLBACK_FROM", "Release rolled back from"),
                    ("ACCEPTANCE_FAILURE", "Acceptance failure"),
                    ("SHAPE_ROLLBACK", "Execution-shape rollback"),
                    ("OPERATOR_DEACTIVATION", "Operator deactivation"),
                ],
                default="",
                max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="validatorexecutiondeployment",
            name="provider_deleted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="validatorexecutiondeployment",
            name="retired_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="validatorexecutiondeployment",
            name="retirement_reason",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddIndex(
            model_name="validatorexecutiondeployment",
            index=models.Index(
                fields=["backend_slug", "backend_release_identity"],
                name="val_execdep_backend_rel_idx",
            ),
        ),
        migrations.RunPython(
            backfill_validator_backend_contracts,
            migrations.RunPython.noop,
        ),
    ]
