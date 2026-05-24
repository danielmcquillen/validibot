"""Rename ``step.config["display_signals"]`` → ``step.config["display_step_outputs"]``.

The Python field, form field, URL slug, view class, and JSON config
key were renamed in Phase 2b of the May 2026 cleanup pass per
ADR-2026-05-22b. The new name disambiguates the value (a list of
contract_keys to display) from the broader "signal" vocabulary, which
the ADR reserved for workflow-level values (``s.*``). The contents
of this list are step output contract_keys, hence "step_outputs".

This data migration rewrites the JSON key on every existing
``WorkflowStep.config`` row. Empty configs and configs without the
key are left alone. Already-renamed configs (the new key already
present) are also left alone — idempotent.

The migration is intentionally one-shot rather than a backward-
compatible shim: per the project's pre-launch posture, there are no
production consumers reading the old key, so dual-format support
would be carrying weight for no payoff. If you discover stray
external consumers (manifest readers, exports) that still reference
the old name, fix those references rather than reintroducing the
shim — a single canonical name is healthier long-term.
"""

from __future__ import annotations

from django.db import migrations


def rename_key_forwards(apps, schema_editor):
    """Rewrite step.config: ``display_signals`` → ``display_step_outputs``."""
    workflow_step = apps.get_model("workflows", "WorkflowStep")
    to_update = []
    # Filtering by JSONField key presence keeps the rewrite bounded to
    # rows that actually have the old key — avoids touching every row
    # in the table. ``contains`` on a JSONField with a dict-shaped
    # argument matches rows whose config dict contains the key.
    for step in workflow_step.objects.filter(config__has_key="display_signals"):
        config = step.config or {}
        if "display_signals" not in config:
            continue
        value = config.pop("display_signals")
        # If both keys somehow co-exist (e.g. a partial earlier
        # migration), the new key wins — it's the authoritative shape.
        config.setdefault("display_step_outputs", value)
        step.config = config
        to_update.append(step)
    if to_update:
        workflow_step.objects.bulk_update(to_update, ["config"])


def rename_key_backwards(apps, schema_editor):
    """Reverse: ``display_step_outputs`` → ``display_signals``.

    Symmetric so the migration is reversible for emergency rollback
    or for branches that haven't picked up the rename yet.
    """
    workflow_step = apps.get_model("workflows", "WorkflowStep")
    to_update = []
    for step in workflow_step.objects.filter(config__has_key="display_step_outputs"):
        config = step.config or {}
        if "display_step_outputs" not in config:
            continue
        value = config.pop("display_step_outputs")
        config.setdefault("display_signals", value)
        step.config = config
        to_update.append(step)
    if to_update:
        workflow_step.objects.bulk_update(to_update, ["config"])


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0026_alter_workflow_version_positive_integer"),
    ]

    operations = [
        migrations.RunPython(
            rename_key_forwards,
            reverse_code=rename_key_backwards,
        ),
    ]
