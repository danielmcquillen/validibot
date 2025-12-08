from __future__ import annotations

import json

from django.db import migrations, models


def forward_copy_rules(apps, schema_editor):
    Ruleset = apps.get_model("validations", "Ruleset")
    for ruleset in Ruleset.objects.all():  # noqa: PERF102 - small dataset per migration
        metadata = dict(ruleset.metadata or {})
        schema_value = metadata.pop("schema", None)
        update_fields: list[str] = []
        if schema_value is not None:
            ruleset.metadata = metadata
            update_fields.append("metadata")
            if not getattr(ruleset, "rules_file", None):
                text_value = (
                    json.dumps(schema_value)
                    if isinstance(schema_value, (dict, list))
                    else str(schema_value)
                )
                if not ruleset.rules_text:
                    ruleset.rules_text = text_value
                    update_fields.append("rules_text")
        if update_fields:
            ruleset.save(update_fields=update_fields)


def backward_copy_rules(apps, schema_editor):
    Ruleset = apps.get_model("validations", "Ruleset")
    for ruleset in Ruleset.objects.all():  # noqa: PERF102 - small dataset per migration
        metadata = dict(ruleset.metadata or {})
        if "schema" not in metadata and ruleset.rules_text and not getattr(ruleset, "rules_file", None):
            try:
                metadata["schema"] = json.loads(ruleset.rules_text)
            except json.JSONDecodeError:
                metadata["schema"] = ruleset.rules_text
        ruleset.metadata = metadata
        ruleset.save(update_fields=["metadata"])


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0002_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="ruleset",
            old_name="file",
            new_name="rules_file",
        ),
        migrations.AddField(
            model_name="ruleset",
            name="rules_text",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Inline ruleset definition (for example, JSON Schema or XML schema text). Use this when you prefer to paste or store the rules without uploading a file.",
            ),
        ),
        migrations.AlterField(
            model_name="ruleset",
            name="rules_file",
            field=models.FileField(
                blank=True,
                help_text="Optional uploaded file containing the ruleset definition. Leave empty when pasting rules directly.",
                upload_to="rulesets/",
            ),
        ),
        migrations.AlterField(
            model_name="ruleset",
            name="metadata",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Additional metadata about the ruleset (non-rule data only).",
            ),
        ),
        migrations.RunPython(forward_copy_rules, backward_copy_rules),
    ]
