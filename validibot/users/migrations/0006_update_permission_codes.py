from django.db import migrations

NEW_PERMISSIONS = (
    ("validation_results_view_all", "Can view all validation results", "validations", "validationrun"),
    ("validation_results_view_own", "Can view own validation results", "validations", "validationrun"),
    ("validator_view", "Can view validators", "validations", "validator"),
    ("validator_edit", "Can create or edit validators", "validations", "validator"),
    ("analytics_view", "Can view analytics", "validations", "validationrun"),
    ("analytics_review", "Can review analytics", "validations", "validationrun"),
)

OLD_PERMISSIONS = (
    ("results_view_all", "validations", "validationrun"),
    ("results_view_own", "validations", "validationrun"),
    ("results_review", "validations", "validationrun"),
)


def create_new_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    for codename, name, app_label, model in NEW_PERMISSIONS:
        content_type, _ = ContentType.objects.get_or_create(
            app_label=app_label,
            model=model,
        )
        Permission.objects.update_or_create(
            codename=codename,
            content_type=content_type,
            defaults={"name": name},
        )


def remove_old_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    for codename, app_label, model in OLD_PERMISSIONS:
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
        ("users", "0005_permission_definitions"),
    ]

    operations = [
        migrations.RunPython(create_new_permissions, remove_old_permissions),
    ]
