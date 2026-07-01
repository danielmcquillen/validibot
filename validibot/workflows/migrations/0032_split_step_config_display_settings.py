"""Move cosmetic keys out of ``WorkflowStep.config`` into ``display_settings``.

ADR-2026-06-18 split the single ``config`` JSONField into a **semantic** bucket
(``config``, hashed wholesale) and a **cosmetic + runtime-injected** bucket
(``display_settings``, never hashed). This data migration reshapes existing rows
to match: for every validator step it keeps only that validator's *semantic*
keys in ``config`` and moves everything else (labels, previews, counts,
``display_step_outputs``, the legacy ``template_variables`` list, and any stray
key) into ``display_settings``.

Routing *everything non-semantic* — not just a hand-listed set of cosmetic keys —
is deliberate: it guarantees ``config`` ends up provably semantic-only so the
follow-up flip of the config Pydantic models to ``extra="forbid"`` can't reject a
pre-existing row.

The semantic field sets are frozen here (a snapshot of ``step_configs.py`` at
migration time) so this migration stays reproducible even if the live models
gain fields later. Action steps (no validator) are left untouched — they are
excluded from the workflow-definition hash and their config models stay
permissive.
"""

from __future__ import annotations

from django.db import migrations

# Frozen snapshot of the SEMANTIC keys per validator type (mirrors
# STEP_CONFIG_MODELS in workflows/step_configs.py as of ADR-2026-06-18). Any key
# NOT listed here is cosmetic/runtime/legacy and moves to display_settings.
SEMANTIC_FIELDS_BY_TYPE: dict[str, set[str]] = {
    "JSON_SCHEMA": {"schema_type"},
    "XML_SCHEMA": {"schema_type"},
    "TABULAR": {"delimiter", "encoding", "has_header"},
    "SHACL": {
        "bundled_standards",
        "inference_mode",
        "advanced_shacl",
        "submission_format",
        "shacl_result_handling",
    },
    "ENERGYPLUS": {
        "validation_mode",
        "idf_checks",
        "run_simulation",
        "timestep_per_hour",
        "case_sensitive",
    },
    "FMU": {"fmu_simulation", "fmu_introspection"},
    "AI_ASSIST": {"template", "mode", "cost_cap_cents", "selectors", "policy_rules"},
    # Basic/Custom validators have no semantic step config (assertions live on
    # the Ruleset), so every key they carry is cosmetic and moves out.
    "BASIC": set(),
    "CUSTOM_VALIDATOR": set(),
}


def split_config(apps, schema_editor):
    """Partition each validator step's ``config`` into the two buckets."""
    WorkflowStep = apps.get_model("workflows", "WorkflowStep")
    steps = WorkflowStep.objects.select_related("validator").iterator()
    for step in steps:
        config = step.config or {}
        if not config:
            continue
        validator = step.validator
        vtype = getattr(validator, "validation_type", None) if validator else None
        # Action steps (no validator) and unrecognised validator types keep their
        # config as-is: actions aren't hashed, and an unknown type has no frozen
        # semantic set to partition against.
        if vtype is None or vtype not in SEMANTIC_FIELDS_BY_TYPE:
            continue
        semantic = SEMANTIC_FIELDS_BY_TYPE[vtype]
        moved = {key: value for key, value in config.items() if key not in semantic}
        if not moved:
            continue
        new_config = {key: value for key, value in config.items() if key in semantic}
        display = dict(step.display_settings or {})
        # setdefault: never clobber a value already present in display_settings.
        for key, value in moved.items():
            display.setdefault(key, value)
        step.config = new_config
        step.display_settings = display
        step.save(update_fields=["config", "display_settings"])


def merge_back(apps, schema_editor):
    """Reverse: fold ``display_settings`` back into ``config`` (best-effort).

    The forward split is lossy of *which* bucket a key came from, so the reverse
    simply re-merges everything into ``config`` and clears ``display_settings``,
    restoring the pre-split single-blob shape.
    """
    WorkflowStep = apps.get_model("workflows", "WorkflowStep")
    for step in WorkflowStep.objects.iterator():
        display = step.display_settings or {}
        if not display:
            continue
        merged = dict(step.config or {})
        for key, value in display.items():
            merged.setdefault(key, value)
        step.config = merged
        step.display_settings = {}
        step.save(update_fields=["config", "display_settings"])


class Migration(migrations.Migration):

    dependencies = [
        ("workflows", "0031_workflowstep_display_settings"),
    ]

    operations = [
        migrations.RunPython(split_config, merge_back),
    ]
