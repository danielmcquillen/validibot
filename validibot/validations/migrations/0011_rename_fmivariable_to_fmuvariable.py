"""
Rename FMIVariable model to FMUVariable and fmi_version field to fmu_version.

Part of the broader FMI -> FMU rename. The FMIKind and FMUVersion inner
classes on FMUModel are renamed in Python only (their stored values don't
change).
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0010_rename_fmi_to_fmu"),
    ]

    operations = [
        # Rename the model (table: validations_fmivariable -> validations_fmuvariable)
        migrations.RenameModel(
            old_name="FMIVariable",
            new_name="FMUVariable",
        ),
        # Rename the field on FMUModel
        migrations.RenameField(
            model_name="fmumodel",
            old_name="fmi_version",
            new_name="fmu_version",
        ),
    ]
