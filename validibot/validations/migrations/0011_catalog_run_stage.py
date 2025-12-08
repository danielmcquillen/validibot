from django.db import migrations
from django.db import models


def forwards(apps, schema_editor):
    Entry = apps.get_model("validations", "ValidatorCatalogEntry")
    for entry in Entry.objects.all():
        if entry.entry_type == "signal_input":
            entry.entry_type = "signal"
            entry.run_stage = "input"
        elif entry.entry_type == "signal_output":
            entry.entry_type = "signal"
            entry.run_stage = "output"
        else:
            entry.entry_type = "derivation"
            entry.run_stage = entry.run_stage or "output"
        entry.save(update_fields=["entry_type", "run_stage"])


def backwards(apps, schema_editor):
    Entry = apps.get_model("validations", "ValidatorCatalogEntry")
    for entry in Entry.objects.all():
        if entry.entry_type == "signal":
            entry.entry_type = (
                "signal_input" if entry.run_stage == "input" else "signal_output"
            )
        # derivations remain derivations regardless of stage
        entry.save(update_fields=["entry_type"])


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0010_alter_ruleset_ruleset_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="validatorcatalogentry",
            name="run_stage",
            field=models.CharField(
                choices=[("input", "Input"), ("output", "Output")],
                default="input",
                help_text="Phase of the validator run when this entry is available.",
                max_length=16,
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.AlterField(
            model_name="validatorcatalogentry",
            name="entry_type",
            field=models.CharField(
                choices=[("signal", "Signal"), ("derivation", "Derivation")],
                max_length=32,
            ),
        ),
    ]
