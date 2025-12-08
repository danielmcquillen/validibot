from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0028_fmi_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="fmumodel",
            name="checksum",
            field=models.CharField(
                blank=True,
                default="",
                help_text="SHA256 checksum used to reference cached FMUs in Modal.",
                max_length=128,
            ),
        ),
        migrations.AddField(
            model_name="fmumodel",
            name="modal_volume_path",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Path inside the Modal Volume cache for this FMU.",
                max_length=255,
            ),
        ),
    ]
