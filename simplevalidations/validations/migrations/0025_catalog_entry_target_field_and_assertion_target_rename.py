from django.db import migrations, models


def backfill_catalog_target(apps, schema_editor):
    ValidatorCatalogEntry = apps.get_model("validations", "ValidatorCatalogEntry")
    for entry in ValidatorCatalogEntry.objects.all():
        if not entry.target_field:
            entry.target_field = entry.slug
            entry.save(update_fields=["target_field"])


def backfill_assertions(apps, schema_editor):
    RulesetAssertion = apps.get_model("validations", "RulesetAssertion")
    for assertion in RulesetAssertion.objects.all():
        if hasattr(assertion, "target_catalog") and assertion.target_catalog_id:
            assertion.target_catalog_entry_id = assertion.target_catalog_id
            assertion.target_catalog_id = None
            assertion.save(
                update_fields=["target_catalog_entry", "target_catalog"],
            )


def noop_reverse(apps, schema_editor):
    # No safe automatic reverse; leave data intact.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0024_validator_has_processor"),
    ]

    operations = [
        migrations.AddField(
            model_name="validatorcatalogentry",
            name="target_field",
            field=models.CharField(
                default="",
                help_text="Path used to locate this signal in the input or processor output.",
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name="validatorcatalogentry",
            name="label",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Human-friendly label shown in editors.",
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name="validatorcatalogentry",
            name="description",
            field=models.TextField(
                blank=True,
                default="",
                help_text="A short description to help you remember what data this signal represents.",
            ),
        ),
        migrations.AlterField(
            model_name="validatorcatalogentry",
            name="is_required",
            field=models.BooleanField(
                default=False,
                help_text="Requires the signal to be present in the submission (inputs) or processor output (outputs).",
            ),
        ),
        migrations.RenameField(
            model_name="rulesetassertion",
            old_name="target_catalog",
            new_name="target_catalog_entry",
        ),
        migrations.RunPython(backfill_catalog_target, noop_reverse),
        migrations.RunPython(backfill_assertions, noop_reverse),
    ]
