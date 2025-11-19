from django.db import migrations

from simplevalidations.submissions.constants import data_format_allowed_file_types
from simplevalidations.validations.constants import ValidationType

BASIC_DEFAULT_FORMATS = [
    "json",
    "xml",
    "yaml",
    "text",
]
LEGACY_BASIC_FORMATS = ["json"]


def _file_types_for_formats(data_formats: list[str]) -> list[str]:
    collected: list[str] = []
    for fmt in data_formats or []:
        for file_type in data_format_allowed_file_types(fmt):
            if file_type not in collected:
                collected.append(file_type)
    return collected or LEGACY_BASIC_FORMATS


def forwards(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    for validator in Validator.objects.filter(validation_type=ValidationType.BASIC):
        formats = validator.supported_data_formats or []
        if not formats or formats == LEGACY_BASIC_FORMATS:
            formats = BASIC_DEFAULT_FORMATS
        validator.supported_data_formats = formats
        validator.supported_file_types = _file_types_for_formats(formats)
        validator.save(update_fields=["supported_data_formats", "supported_file_types"])


def backwards(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    for validator in Validator.objects.filter(validation_type=ValidationType.BASIC):
        formats = validator.supported_data_formats or []
        if formats == BASIC_DEFAULT_FORMATS:
            formats = LEGACY_BASIC_FORMATS
        validator.supported_data_formats = formats
        validator.supported_file_types = _file_types_for_formats(formats)
        validator.save(update_fields=["supported_data_formats", "supported_file_types"])


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0021_alter_customvalidator_base_validation_type_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
