from django.db import migrations


def set_basic_allow(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    Validator.objects.filter(validation_type="BASIC").update(
        allow_custom_assertion_targets=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0008_alter_customvalidator_base_validation_type_and_more"),
    ]

    operations = [
        migrations.RunPython(set_basic_allow, migrations.RunPython.noop),
    ]
