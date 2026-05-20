"""Backfill empty ``Workflow.version`` rows to a non-colliding integer label.

Why
---
``Workflow.version`` is moving from ``blank=True, default=""`` to a required
field. Before the schema migration tightens the column, any existing rows
that carry an empty version need a real label so the
``(org, slug, version)`` unique constraint holds and the version-arithmetic
helpers in ``workflows.version_utils`` keep working.

Strategy
--------
For every workflow with an empty version, walk integers starting at ``1``
and assign the first label that does not already exist in the row's
``(org, slug)`` family. The pattern mirrors
``0021_normalize_workflow_version_labels`` which uses the same trick for
partial-semver normalization.

A two-empty case in the same family is structurally impossible today
because the unique constraint added by an earlier migration already
prevents it (Postgres treats empty strings as values for uniqueness
purposes). But we still iterate defensively so the migration is correct
even if the constraint hasn't been applied in some unusual deploy state.

The schema tightening (``blank=False``, ``default="1"``) is a separate
migration immediately after this one. Splitting them means that if
backfill fails midway, the schema stays permissive and the deploy can
recover without rolling back a schema change.
"""

from __future__ import annotations

from django.db import migrations


def backfill_empty_versions(apps, schema_editor):
    """Assign every blank ``Workflow.version`` the first free integer label."""
    workflow_model = apps.get_model("workflows", "Workflow")

    blanks = workflow_model.objects.filter(version="").only(
        "id",
        "org_id",
        "slug",
        "version",
    )
    for workflow in blanks:
        candidate = 1
        while True:
            label = str(candidate)
            collision = (
                workflow_model.objects.filter(
                    org_id=workflow.org_id,
                    slug=workflow.slug,
                    version=label,
                )
                .exclude(pk=workflow.pk)
                .exists()
            )
            if not collision:
                break
            candidate += 1

        workflow.version = label
        workflow.save(update_fields=["version"])


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0022_alter_workflow_version"),
    ]

    operations = [
        migrations.RunPython(
            backfill_empty_versions,
            migrations.RunPython.noop,
        ),
    ]
