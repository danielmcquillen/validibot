from django.db import migrations, models

from validibot.users.constants import RoleCode


def add_roles(apps, schema_editor):
    Role = apps.get_model("users", "Role")
    # Ensure analytics viewer exists
    Role.objects.get_or_create(
        code=RoleCode.ANALYTICS_VIEWER,
        defaults={"name": RoleCode.ANALYTICS_VIEWER.label},
    )
    # Rename results viewer to validation results viewer
    Role.objects.filter(code="RESULTS_VIEWER").update(
        code=RoleCode.VALIDATION_RESULTS_VIEWER,
        name=RoleCode.VALIDATION_RESULTS_VIEWER.label,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0006_update_permission_codes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="role",
            name="code",
            field=models.CharField(
                choices=[
                    ("OWNER", "Owner"),
                    ("ADMIN", "Admin"),
                    ("AUTHOR", "Author"),
                    ("EXECUTOR", "Executor"),
                    ("ANALYTICS_VIEWER", "Analytics Viewer"),
                    ("WORKFLOW_VIEWER", "Workflow Viewer"),
                    ("VALIDATION_RESULTS_VIEWER", "Validation Results Viewer"),
                ],
                default="WORKFLOW_VIEWER",
                max_length=32,
            ),
        ),
        migrations.RunPython(add_roles, migrations.RunPython.noop),
    ]
