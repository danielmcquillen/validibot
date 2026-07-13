import django.db.models.deletion
from django.db import migrations
from django.db import models


def delete_unbound_callback_receipts(apps, schema_editor):
    """Discard rollout-era receipts that cannot identify provider work.

    Callback receipts are short-lived idempotency records, not validation
    evidence. Before production adoption there is no reason to preserve rows
    created by the retired callback path without an execution attempt.
    """
    callback_receipt = apps.get_model("validations", "CallbackReceipt")
    callback_receipt.objects.filter(execution_attempt__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0065_validationrun_runtime_profile_executionattempt_and_more"),
    ]

    operations = [
        migrations.RunPython(
            delete_unbound_callback_receipts,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="callbackreceipt",
            name="execution_attempt",
            field=models.ForeignKey(
                help_text="The concrete execution that produced this callback.",
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="callback_receipts",
                to="validations.executionattempt",
            ),
        ),
        migrations.AlterField(
            model_name="validationrun",
            name="runtime_profile",
            field=models.CharField(
                choices=[
                    (
                        "ATTEMPT_LIFECYCLE_V1",
                        "Execution attempt lifecycle v1",
                    ),
                    (
                        "ATTEMPT_STRICT_V1",
                        "Strict execution attempt I/O v1",
                    ),
                    (
                        "ATTEMPT_CONTEXT_V1",
                        "Canonical run context v1",
                    ),
                ],
                default="ATTEMPT_LIFECYCLE_V1",
                editable=False,
                help_text=(
                    "Immutable execution semantics selected when this run was created."
                ),
                max_length=32,
            ),
        ),
    ]
