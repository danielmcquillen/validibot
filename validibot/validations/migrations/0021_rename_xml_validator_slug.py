"""
Rename the XML validator slug from ``xml-schema-validator`` to
``xml-validator`` in the database.

The slug was changed in code (commit 9b4b239) but without a
corresponding data migration. On existing deployments the old-slug row
still exists, and ``sync_validators`` / ``create_default_validators``
would create a *new* row for the new slug — resulting in duplicate XML
validators.

This migration handles three cases:
  1. Only old slug exists -> rename it.
  2. Only new slug exists -> nothing to do (fresh install or already synced).
  3. Both exist -> keep the old row (which has existing workflow step
     references via ForeignKey), update its slug, and delete the
     duplicate new row.
"""

from django.db import migrations

OLD_SLUG = "xml-schema-validator"
NEW_SLUG = "xml-validator"


def rename_xml_slug(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")

    old_exists = Validator.objects.filter(slug=OLD_SLUG).exists()
    new_exists = Validator.objects.filter(slug=NEW_SLUG).exists()

    if old_exists and new_exists:
        # Both rows exist. The old row has FK references from workflow
        # steps, so keep it and delete the duplicate new row.
        Validator.objects.filter(slug=NEW_SLUG).delete()
        Validator.objects.filter(slug=OLD_SLUG).update(slug=NEW_SLUG)
        print(
            f"  Merged duplicate XML validators: "
            f"deleted '{NEW_SLUG}' row, renamed '{OLD_SLUG}' -> '{NEW_SLUG}'."
        )
    elif old_exists:
        Validator.objects.filter(slug=OLD_SLUG).update(slug=NEW_SLUG)
        print(f"  Renamed XML validator slug: '{OLD_SLUG}' -> '{NEW_SLUG}'.")
    else:
        # Only new slug exists (or neither) — nothing to do.
        pass


def reverse_rename(apps, schema_editor):
    Validator = apps.get_model("validations", "Validator")
    Validator.objects.filter(slug=NEW_SLUG).update(slug=OLD_SLUG)


class Migration(migrations.Migration):

    dependencies = [
        ("validations", "0020_backfill_supports_assertions"),
    ]

    operations = [
        migrations.RunPython(
            rename_xml_slug,
            reverse_code=reverse_rename,
        ),
    ]
