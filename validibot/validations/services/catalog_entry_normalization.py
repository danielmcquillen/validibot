"""
Helpers for normalizing legacy catalog-entry binding metadata.

System validator configs still declare a single ``binding_config`` dict
on each catalog entry. Historically that dict mixed two different
concerns:

- provider-native runtime metadata (for example an EnergyPlus metric key)
- submission-source defaults (for example ``submission.metadata`` path)

The unified step I/O model stores those concerns separately:

- ``StepIODefinition.provider_binding`` holds provider-facing runtime hints
- ``StepInputBinding`` holds per-step submission sourcing defaults

These helpers translate the legacy mixed mapping into those two
representations so the database stays aligned with the step I/O
model without forcing every validator config to change at
once.
"""

from __future__ import annotations

from typing import Any

from validibot.validations.constants import BindingSourceScope

LEGACY_SOURCE_SCOPE_MAP = {
    "submission": BindingSourceScope.SUBMISSION_PAYLOAD,
    "payload": BindingSourceScope.SUBMISSION_PAYLOAD,
    "submission.payload": BindingSourceScope.SUBMISSION_PAYLOAD,
    "submission_payload": BindingSourceScope.SUBMISSION_PAYLOAD,
    "submission.metadata": BindingSourceScope.SUBMISSION_METADATA,
    "submission_metadata": BindingSourceScope.SUBMISSION_METADATA,
    "system": BindingSourceScope.SYSTEM,
    "upstream_step": BindingSourceScope.UPSTREAM_STEP,
}

_SUBMISSION_SOURCE_KEYS = frozenset(
    {
        "default_value",
        "is_required",
        "path",
        "source",
        "source_data_path",
        "source_scope",
    },
)


def build_step_binding_defaults_from_mapping(
    mapping: dict[str, Any] | None,
    *,
    fallback_path: str,
    default_required: bool,
) -> dict[str, Any]:
    """Return canonical ``StepInputBinding`` defaults from a mixed mapping.

    Args:
        mapping: Legacy catalog-entry binding metadata.
        fallback_path: Path to use when the mapping does not declare one.
        default_required: Required/optional default to use when the mapping
            does not declare ``is_required`` explicitly.

    Returns:
        Dict suitable for ``StepInputBinding(..., **defaults)``.
    """
    binding = dict(mapping or {})
    source = binding.get("source")

    source_scope = (
        binding.get("source_scope")
        or LEGACY_SOURCE_SCOPE_MAP.get(source)
        or BindingSourceScope.SUBMISSION_PAYLOAD
    )

    source_data_path = binding.get("source_data_path")
    if source_data_path in (None, "") and source in LEGACY_SOURCE_SCOPE_MAP:
        source_data_path = binding.get("path")
    if source_data_path in (None, ""):
        source_data_path = fallback_path

    default_value = binding.get("default_value")
    is_required = binding.get("is_required", default_required)

    return {
        "default_value": default_value,
        "is_required": is_required,
        "source_data_path": source_data_path or "",
        "source_scope": source_scope,
    }


def build_provider_binding_from_mapping(
    mapping: dict[str, Any] | None,
) -> dict[str, Any]:
    """Strip submission-source selectors from provider-facing metadata.

    The returned dict is safe to persist in
    ``StepIODefinition.provider_binding`` because it contains only
    provider/runtime hints, not submission lookup details.
    """
    binding = dict(mapping or {})
    source = binding.get("source")

    if source == "metric":
        provider_binding = dict(binding)
        metric_key = provider_binding.pop("metric_key", None) or provider_binding.pop(
            "key", None
        )
        provider_binding.pop("path", None)
        provider_binding.pop("source", None)
        if metric_key:
            provider_binding["metric_key"] = metric_key
        return provider_binding

    if source == "parser":
        # Per ADR-2026-05-22, parser-extracted step inputs declare
        # {"source": "parser", "key": "<contract_key>"} in their
        # binding_config. These values are populated at runtime by the
        # validator's extract_input_values() hook — no payload path or
        # runtime metadata is involved at all, so the provider_binding
        # stored on StepIODefinition is empty.
        return {}

    if (
        source in LEGACY_SOURCE_SCOPE_MAP
        or "source_scope" in binding
        or "source_data_path" in binding
    ):
        provider_binding = dict(binding)
        for key in _SUBMISSION_SOURCE_KEYS:
            provider_binding.pop(key, None)
        return provider_binding

    return binding
