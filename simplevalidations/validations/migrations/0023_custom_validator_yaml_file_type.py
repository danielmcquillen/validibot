from django.db import migrations

from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.validations.constants import ValidationType

DEFAULT_CUSTOM_FILE_TYPES = [
    SubmissionFileType.JSON,
    SubmissionFileType.TEXT,
    SubmissionFileType.YAML,
]


def forwards(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    target = SubmissionFileType.YAML
    for validator in Validator.objects.filter(validation_type=ValidationType.CUSTOM_VALIDATOR):
        file_types = list(validator.supported_file_types or [])
        if target not in file_types:
            file_types.append(target)
        if not file_types:
            file_types = DEFAULT_CUSTOM_FILE_TYPES
        validator.supported_file_types = list(dict.fromkeys(file_types))
        validator.save(update_fields=["supported_file_types"])


def backwards(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    target = SubmissionFileType.YAML
    for validator in Validator.objects.filter(validation_type=ValidationType.CUSTOM_VALIDATOR):
        file_types = [ft for ft in validator.supported_file_types or [] if ft != target]
        if not file_types:
            file_types = DEFAULT_CUSTOM_FILE_TYPES
        validator.supported_file_types = list(dict.fromkeys(file_types))
        validator.save(update_fields=["supported_file_types"])


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0022_expand_basic_supported_formats"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
