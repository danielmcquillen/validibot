from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0025_catalog_entry_target_field_and_assertion_target_rename"),
    ]

    operations = [
        migrations.AlterField(
            model_name="rulesetassertion",
            name="target_catalog_entry",
            field=models.ForeignKey(
                blank=True,
                help_text="Reference to a catalog entry when targeting a known signal.",
                null=True,
                on_delete=models.SET_NULL,
                related_name="ruleset_assertions",
                to="validations.validatorcatalogentry",
            ),
        ),
    ]
