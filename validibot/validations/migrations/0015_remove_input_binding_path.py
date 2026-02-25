"""Remove the unused input_binding_path field from ValidatorCatalogEntry."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0014_rename_target_field_to_target_data_path"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="validatorcatalogentry",
            name="input_binding_path",
        ),
    ]
