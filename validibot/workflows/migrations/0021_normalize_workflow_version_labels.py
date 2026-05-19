from __future__ import annotations

import re

from django.db import migrations

PARTIAL_SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def normalize_partial_semver_labels(apps, schema_editor):
    """Convert pre-strict labels like ``1.0`` to full ``1.0.0`` labels."""
    workflow_model = apps.get_model("workflows", "Workflow")

    workflows = workflow_model.objects.all().only(
        "id",
        "org_id",
        "slug",
        "version",
    )
    for workflow in workflows:
        version = (workflow.version or "").strip()
        match = PARTIAL_SEMVER_PATTERN.match(version)
        if not match:
            continue

        major, minor = match.groups()
        patch = 0
        while True:
            candidate = f"{major}.{minor}.{patch}"
            collision = workflow_model.objects.filter(
                org_id=workflow.org_id,
                slug=workflow.slug,
                version=candidate,
            ).exclude(pk=workflow.pk)
            if not collision.exists():
                break
            patch += 1

        workflow.version = candidate
        workflow.save(update_fields=["version"])


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0020_workflow_history_policy"),
    ]

    operations = [
        migrations.RunPython(
            normalize_partial_semver_labels,
            migrations.RunPython.noop,
        ),
    ]
