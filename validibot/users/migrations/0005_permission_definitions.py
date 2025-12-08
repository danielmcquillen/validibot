from django.db import migrations

PERMISSIONS = (
    ("workflow_launch", "Can launch workflows", "workflows", "workflow"),
    ("results_view_all", "Can view all validation results", "validations", "validationrun"),
    ("results_view_own", "Can view own validation results", "validations", "validationrun"),
    ("workflow_view", "Can view workflows", "workflows", "workflow"),
    ("workflow_edit", "Can edit workflows", "workflows", "workflow"),
    ("results_review", "Can review validation results", "validations", "validationrun"),
    ("admin_manage_org", "Can manage organizations and members", "users", "organization"),
)


def create_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    for codename, name, app_label, model in PERMISSIONS:
        content_type, _ = ContentType.objects.get_or_create(
            app_label=app_label,
            model=model,
        )
        Permission.objects.update_or_create(
            codename=codename,
            content_type=content_type,
            defaults={"name": name},
        )


def remove_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    for codename, _, app_label, model in PERMISSIONS:
        try:
            content_type = ContentType.objects.get(
                app_label=app_label,
                model=model,
            )
        except ContentType.DoesNotExist:
            continue

        Permission.objects.filter(
            codename=codename,
            content_type=content_type,
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0004_alter_role_code"),
        ("workflows", "0014_workflow_is_archived"),
        ("validations", "0036_alter_rulesetassertion_target_catalog_entry"),
    ]

    operations = [
        migrations.RunPython(create_permissions, remove_permissions),
    ]
