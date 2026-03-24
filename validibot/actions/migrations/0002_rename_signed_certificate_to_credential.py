"""Rename SignedCertificateAction → SignedCredentialAction.

Renames the model, field, and updates stored ActionDefinition values
(type, slug, name) to reflect the "signed credential" terminology.
"""

from django.db import migrations


def rename_workflow_step_config_key(apps, old_key, new_key):
    WorkflowStep = apps.get_model("workflows", "WorkflowStep")
    for step in WorkflowStep.objects.exclude(config__isnull=True).iterator():
        config = step.config or {}
        if not isinstance(config, dict) or old_key not in config:
            continue

        updated_config = dict(config)
        if new_key not in updated_config:
            updated_config[new_key] = updated_config[old_key]
        updated_config.pop(old_key, None)
        step.config = updated_config
        step.save(update_fields=["config"])


def rename_certificate_to_credential(apps, schema_editor):
    ActionDefinition = apps.get_model("actions", "ActionDefinition")
    ActionDefinition.objects.filter(type="SIGNED_CERTIFICATE").update(
        type="SIGNED_CREDENTIAL",
        slug="certification-signed-credential",
        name="Signed credential",
        description="Issue a signed credential for successful validations.",
    )
    rename_workflow_step_config_key(
        apps,
        "certificate_template",
        "credential_template",
    )


def rename_credential_to_certificate(apps, schema_editor):
    ActionDefinition = apps.get_model("actions", "ActionDefinition")
    ActionDefinition.objects.filter(type="SIGNED_CREDENTIAL").update(
        type="SIGNED_CERTIFICATE",
        slug="certification-signed-certificate",
        name="Signed certificate",
        description="Issue a signed certificate for successful validations.",
    )
    rename_workflow_step_config_key(
        apps,
        "credential_template",
        "certificate_template",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("actions", "0001_initial"),
        ("workflows", "0008_input_schema_fields"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="SignedCertificateAction",
            new_name="SignedCredentialAction",
        ),
        migrations.RenameField(
            model_name="signedcredentialaction",
            old_name="certificate_template",
            new_name="credential_template",
        ),
        migrations.RunPython(
            rename_certificate_to_credential,
            rename_credential_to_certificate,
        ),
    ]
