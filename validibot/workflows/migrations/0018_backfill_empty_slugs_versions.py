"""
Backfill empty workflow slugs and versions.

This migration ensures all workflows have non-empty slugs and versions
as required by ADR-2026-01-06 (org-scoped API routing).

- Empty slugs are populated from the workflow name (slugified) or a
  generated fallback like "wf-8f3a1c2d"
- Empty versions are set to "1"
"""

import uuid

from django.db import migrations
from django.utils.text import slugify


def backfill_empty_slugs(apps, schema_editor):
    """Populate empty workflow slugs with generated values."""
    Workflow = apps.get_model("workflows", "Workflow")

    for workflow in Workflow.objects.filter(slug=""):
        candidate = slugify(workflow.name)
        if not candidate:
            candidate = f"wf-{uuid.uuid4().hex[:8]}"

        # Ensure uniqueness within org (respecting the unique constraint)
        base_slug = candidate
        counter = 1
        while Workflow.objects.filter(
            org_id=workflow.org_id,
            slug=candidate,
            version=workflow.version,
        ).exclude(pk=workflow.pk).exists():
            candidate = f"{base_slug}-{counter}"
            counter += 1

        workflow.slug = candidate
        workflow.save(update_fields=["slug"])


def backfill_empty_versions(apps, schema_editor):
    """Set empty workflow versions to "1"."""
    Workflow = apps.get_model("workflows", "Workflow")

    Workflow.objects.filter(version="").update(version="1")


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0017_add_workflow_access_grant_and_invite"),
    ]

    operations = [
        migrations.RunPython(backfill_empty_slugs, migrations.RunPython.noop),
        migrations.RunPython(backfill_empty_versions, migrations.RunPython.noop),
    ]
