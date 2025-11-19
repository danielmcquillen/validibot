from django.db import migrations, models


def set_processor_flags(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    energyplus_formats = {"energyplus_idf", "energyplus_epjson", "fmu"}
    for validator in Validator.objects.all():
        if validator.validation_type == "ENERGYPLUS":
            validator.has_processor = True
        elif any(fmt in (validator.supported_data_formats or []) for fmt in energyplus_formats):
            validator.has_processor = True
        validator.save(update_fields=["has_processor"])


def unset_processor_flags(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    Validator.objects.update(has_processor=False)


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0023_custom_validator_yaml_file_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="validator",
            name="has_processor",
            field=models.BooleanField(
                default=False,
                help_text="True when the validator includes an intermediate processor that produces output signals.",
            ),
        ),
        migrations.RunPython(set_processor_flags, unset_processor_flags),
    ]
