"""Export a live Workflow to a portable definition (+ bundled files).

The exporter walks the same Workflow → Step graph that
``WorkflowVersioningService.clone`` copies, but instead of writing new rows it
emits a plain-dict ``workflow.json`` definition plus a ``{hash: bytes}`` map of
step-owned binary resources. The validator-specific body of each step (its
ruleset + assertions) is delegated to the step's :class:`StepSerializer`, so a
new validator's export support is its serializer, not a change here.

Identity, ownership, and lifecycle are deliberately *not* exported: a workflow's
``uuid``/``slug``/``version``, its ``org``/``user``/``project``, and its
locked/active state are all minted or rebound at import time. What travels is the
*shape* — fields, steps, rulesets, assertions, signals, resources — not the
particular row it came from.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.validators.base.step_serializer import get_step_serializer
from validibot.workflows.services.io import schema
from validibot.workflows.services.io import vaf

if TYPE_CHECKING:
    from validibot.workflows.models import Workflow
    from validibot.workflows.models import WorkflowStep


def export_definition(workflow: Workflow) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Serialize *workflow* to ``(definition_dict, files)``.

    ``definition_dict`` is the ``workflow.json`` payload; ``files`` maps content
    hash -> bytes for every step-owned resource the definition references. A
    file-free workflow (the common case, including the Darwin Core example)
    returns an empty ``files`` map, so its definition is independently importable
    as bare JSON.
    """
    files: dict[str, bytes] = {}
    definition = {
        "format_version": schema.FORMAT_VERSION,
        "workflow": _export_workflow_fields(workflow),
        "steps": [_export_step(step, files) for step in _ordered_steps(workflow)],
    }
    return definition, files


def export_to_vaf(
    workflow: Workflow,
    *,
    exported_by: str = "",
    exported_at: str = "",
    app_version: str = "",
) -> bytes:
    """Serialize *workflow* and pack it into ``.vaf`` archive bytes.

    Provenance (who/when/version) is passed in rather than read from a clock here
    so this stays a pure transform; the caller (view/command) stamps it.
    """
    definition, files = export_definition(workflow)
    manifest_extra = {
        key: value
        for key, value in (
            ("exported_at", exported_at),
            ("exported_by", exported_by),
            ("validibot_version", app_version),
            ("workflow_name", workflow.name),
        )
        if value
    }
    return vaf.pack(definition, files=files, manifest_extra=manifest_extra)


# ─────────────────────────────────────────────────────────── internals ──


def _ordered_steps(workflow: Workflow) -> list[WorkflowStep]:
    """Return the workflow's steps in run order, with related rows prefetched."""
    return list(
        workflow.steps.select_related("ruleset", "validator", "action")
        .prefetch_related(
            "signal_definitions",
            "signal_bindings__signal_definition",
            "derivations",
            "io_promotions__signal_definition",
            "step_resources",
        )
        .order_by("order"),
    )


def _export_workflow_fields(workflow: Workflow) -> dict[str, Any]:
    """Serialize the workflow's contract fields plus public info / signals."""
    data: dict[str, Any] = {
        field: getattr(workflow, field) for field in schema.WORKFLOW_SCALAR_FIELDS
    }
    data["slug"] = workflow.slug  # informational; importer mints a unique one
    data["allowed_file_types"] = list(workflow.allowed_file_types or [])
    data["input_schema"] = deepcopy(workflow.input_schema)
    data["public_info"] = _export_public_info(workflow)
    data["signal_mappings"] = _export_signal_mappings(workflow)
    return data


def _export_public_info(workflow: Workflow) -> dict[str, Any] | None:
    """Serialize WorkflowPublicInfo (content_html is recomputed on import)."""
    from validibot.workflows.models import WorkflowPublicInfo

    try:
        info = workflow.public_info
    except WorkflowPublicInfo.DoesNotExist:
        return None
    return {
        "title": info.title,
        "content_md": info.content_md,
        "show_steps": info.show_steps,
    }


def _export_signal_mappings(workflow: Workflow) -> list[dict[str, Any]]:
    """Serialize workflow-level signal mappings (s.* vocabulary)."""
    mappings = []
    for mapping in workflow.signal_mappings.all().order_by("position", "pk"):
        row = {field: getattr(mapping, field) for field in schema.SIGNAL_MAPPING_FIELDS}
        row["default_value"] = deepcopy(mapping.default_value)
        mappings.append(row)
    return mappings


def _export_step(step: WorkflowStep, files: dict[str, bytes]) -> dict[str, Any]:
    """Serialize one step: scalars, validator/action ref, ruleset, signals, etc."""
    data: dict[str, Any] = {
        field: getattr(step, field) for field in schema.STEP_SCALAR_FIELDS
    }
    data["config"] = deepcopy(step.config) or {}

    if step.action_id:
        # Action steps aren't part of the validator graph; record enough to
        # report a clear "not supported" on import rather than silently dropping.
        data["kind"] = "action"
        data["action_ref"] = {"slug": getattr(step.action, "slug", "")}
        return data

    data["kind"] = "validator"
    data["validator_ref"] = _export_validator_ref(step)
    serializer = get_step_serializer(getattr(step.validator, "validation_type", ""))
    data["ruleset"] = serializer.export_ruleset(step.ruleset, files=files)
    data["signal_definitions"] = _export_signal_definitions(step)
    data["signal_bindings"] = _export_signal_bindings(step)
    data["derivations"] = _export_derivations(step)
    data["io_promotions"] = _export_io_promotions(step)
    data["resources"] = _export_resources(step, files)
    return data


def _export_validator_ref(step: WorkflowStep) -> dict[str, Any]:
    """Serialize the stable reference used to resolve the validator on import."""
    validator = step.validator
    return {
        "validation_type": validator.validation_type,
        "slug": validator.slug,
        "version": validator.version,
        "is_system": validator.is_system,
        "name": validator.name,
    }


def _export_signal_definitions(step: WorkflowStep) -> list[dict[str, Any]]:
    """Serialize step-owned signal definitions (validator-owned ones are shared)."""
    rows = []
    for signal in step.signal_definitions.all().order_by("order", "pk"):
        row = {
            field: getattr(signal, field) for field in schema.SIGNAL_DEFINITION_FIELDS
        }
        for json_field in schema.SIGNAL_DEFINITION_JSON_FIELDS:
            row[json_field] = deepcopy(getattr(signal, json_field)) or {}
        rows.append(row)
    return rows


def _export_signal_bindings(step: WorkflowStep) -> list[dict[str, Any]]:
    """Serialize step input bindings with a re-bindable signal reference."""
    from validibot.validations.validators.base.step_serializer import _export_signal_ref

    rows = []
    for binding in step.signal_bindings.all().order_by("pk"):
        row = {field: getattr(binding, field) for field in schema.SIGNAL_BINDING_FIELDS}
        row["default_value"] = deepcopy(binding.default_value)
        row["signal_ref"] = _export_signal_ref(binding.signal_definition)
        rows.append(row)
    return rows


def _export_derivations(step: WorkflowStep) -> list[dict[str, Any]]:
    """Serialize step-owned derivations."""
    rows = []
    for derivation in step.derivations.all().order_by("order", "pk"):
        row = {field: getattr(derivation, field) for field in schema.DERIVATION_FIELDS}
        row["metadata"] = deepcopy(derivation.metadata) or {}
        rows.append(row)
    return rows


def _export_io_promotions(step: WorkflowStep) -> list[dict[str, Any]]:
    """Serialize signal->s.* promotion overlays."""
    from validibot.validations.validators.base.step_serializer import _export_signal_ref

    rows = []
    for promotion in step.io_promotions.all().order_by("pk"):
        rows.append(
            {
                "promoted_signal_name": promotion.promoted_signal_name,
                "signal_ref": _export_signal_ref(promotion.signal_definition),
            },
        )
    return rows


def _export_resources(
    step: WorkflowStep,
    files: dict[str, bytes],
) -> list[dict[str, Any]]:
    """Serialize step resources: catalog references and bundled step-owned files."""
    rows: list[dict[str, Any]] = []
    for resource in step.step_resources.all().order_by("pk"):
        if resource.is_catalog_reference:
            catalog = resource.validator_resource_file
            rows.append(
                {
                    "role": resource.role,
                    "mode": "catalog",
                    "catalog_ref": {
                        "filename": getattr(catalog, "filename", "")
                        or getattr(catalog, "name", ""),
                        "role": resource.role,
                    },
                },
            )
            continue
        with resource.step_resource_file.open("rb") as handle:
            payload = handle.read()
        file_hash = vaf.content_hash(payload)
        files[file_hash] = payload
        rows.append(
            {
                "role": resource.role,
                "mode": "owned",
                "filename": resource.filename,
                "resource_type": resource.resource_type,
                "content_ref": file_hash,
            },
        )
    return rows
