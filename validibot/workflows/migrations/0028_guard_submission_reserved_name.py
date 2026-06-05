"""One-time guard: no pre-existing signal may be named ``submission``.

ADR-2026-06-03b adds ``submission`` as the sixth top-level CEL/assertion
namespace and reserves the name (it is now a member of ``RESERVED_CEL_NAMES``
via ``CEL_NAMESPACE_ROOTS``). New signals can no longer be named
``submission``, but a signal could have been created BEFORE the name was
reserved. This migration catches any such row — a workflow signal mapping or a
step-IO promotion called ``submission`` — and refuses to migrate until it is
renamed, with a clear remediation message.

The check is genuinely one-time: once no such row exists it is a no-op on every
subsequent deploy. The likelihood of a hit is near zero (``submission`` was
never an obvious signal name), but a silent overlap between an author's
``s.submission`` and the new ``submission.`` namespace would be confusing, so
we fail loudly here rather than let it slip in.

It lives in the ``workflows`` app because ``WorkflowSignalMapping`` does, and
depends on the latest ``validations`` migration so the promotion models are
available in the historical app state.
"""

from __future__ import annotations

from django.db import migrations

# The reserved name being guarded. Kept as a literal (not imported from
# application code) so the migration stays stable even if the constant moves.
_RESERVED = "submission"


def _guard_no_submission_named_signal(apps, schema_editor):
    """Raise if any signal mapping or promotion is named ``submission``."""
    workflow_signal_mapping = apps.get_model("workflows", "WorkflowSignalMapping")
    step_io_definition = apps.get_model("validations", "StepIODefinition")
    step_io_promotion = apps.get_model("validations", "WorkflowStepIOPromotion")

    offenders: list[str] = []
    offenders += [
        f"WorkflowSignalMapping(id={pk}, workflow_id={wf})"
        for pk, wf in workflow_signal_mapping.objects.filter(
            name=_RESERVED,
        ).values_list("id", "workflow_id")
    ]
    offenders += [
        f"StepIODefinition(id={pk}) promoted_signal_name='{_RESERVED}'"
        for pk in step_io_definition.objects.filter(
            promoted_signal_name=_RESERVED,
        ).values_list("id", flat=True)
    ]
    offenders += [
        f"WorkflowStepIOPromotion(id={pk}) promoted_signal_name='{_RESERVED}'"
        for pk in step_io_promotion.objects.filter(
            promoted_signal_name=_RESERVED,
        ).values_list("id", flat=True)
    ]

    if offenders:
        listing = "\n  - ".join(offenders)
        msg = (
            f"Cannot migrate: '{_RESERVED}' is now a reserved CEL namespace "
            f"(ADR-2026-06-03b) and can no longer be used as a signal or "
            f"promotion name, but these rows still use it:\n  - "
            f"{listing}\n\n"
            f"Remediation: rename each to a non-reserved name (for example "
            f"'submission_info') in the Signals / Step-IO editor, then re-run "
            f"the migration. Reserved names are the CEL namespace roots plus "
            f"CEL built-ins (see RESERVED_CEL_NAMES)."
        )
        raise RuntimeError(msg)


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0027_rename_display_signals_config_key"),
        ("validations", "0060_rulesetassertion_notes"),
    ]

    operations = [
        # Forward: scan-and-guard. Reverse: nothing to undo (no data changed).
        migrations.RunPython(
            _guard_no_submission_named_signal,
            migrations.RunPython.noop,
        ),
    ]
