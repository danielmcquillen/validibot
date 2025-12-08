from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0037_remove_validationrun_resolved_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="fmumodel",
            name="gcs_uri",
            field=models.CharField(
                blank=True,
                default="",
                help_text="GCS URI to the canonical FMU object (e.g., gs://bucket/fmus/<checksum>.fmu).",
                max_length=512,
            ),
        ),
        migrations.RemoveField(
            model_name="fmumodel",
            name="modal_volume_path",
        ),
        migrations.AddField(
            model_name="validatorcatalogentry",
            name="input_binding_path",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Optional path to the submission field used for this input signal. "
                    "If empty, the system uses the catalog slug as a top-level key. "
                    "Workflow authors cannot override this binding."
                ),
                max_length=255,
            ),
        ),
    ]
