"""Rename target_field to target_data_path on RulesetAssertion and ValidatorCatalogEntry."""

from django.db import migrations
from django.db import models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0013_rename_fmi_variables_related_name"),
    ]

    operations = [
        migrations.RenameField(
            model_name="rulesetassertion",
            old_name="target_field",
            new_name="target_data_path",
        ),
        migrations.RenameField(
            model_name="validatorcatalogentry",
            old_name="target_field",
            new_name="target_data_path",
        ),
        # Recreate the check constraint with the new field name.
        migrations.RemoveConstraint(
            model_name="rulesetassertion",
            name="ck_ruleset_assertion_target_oneof",
        ),
        migrations.AddConstraint(
            model_name="rulesetassertion",
            constraint=models.CheckConstraint(
                name="ck_ruleset_assertion_target_oneof",
                condition=(
                    Q(target_catalog_entry__isnull=False, target_data_path="")
                    | (Q(target_catalog_entry__isnull=True) & ~Q(target_data_path=""))
                ),
            ),
        ),
    ]
