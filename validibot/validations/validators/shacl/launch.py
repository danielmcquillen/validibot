"""Django-side resolution of SHACL settings into a container input payload.

The SHACL validator now runs in an isolated container (``validibot-validator-
backends``). The container has no database, so everything it needs — the merged
shapes/ontology text, the resolved engine knobs, the resource limits, and the
author-defined SPARQL-ASK assertions — must be resolved here, in Django, and
shipped in the input envelope's ``SHACLInputs``.

This module owns that resolution. It reuses the pure helpers that remain in
:mod:`engine` (shape merging, serialization detection) and reads the operator's
``SHACL_*`` settings, clamping each to the same hard caps the in-process engine
used so a misconfigured setting can't widen the container's safety net.

The actual RDF parsing / pyshacl / SPARQL execution lives ONLY in the container —
see ``validibot-validator-backends/validator_backends/shacl/``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings as django_settings
from validibot_shared.shacl.envelopes import SHACLInputs
from validibot_shared.shacl.envelopes import SHACLSparqlAssertionSpec

from validibot.validations.constants import AssertionType
from validibot.validations.constants import Severity
from validibot.validations.validators.shacl import engine
from validibot.validations.validators.shacl.constants import (
    SHACL_RESULT_HANDLING_DEFAULT,
)
from validibot.validations.validators.shacl.constants import (
    SHACL_RESULT_HANDLING_VALUES,
)

if TYPE_CHECKING:
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator

logger = logging.getLogger(__name__)

# Hard caps mirror the in-process engine's ADR-2026-05-18 table. Django clamps to
# these before shipping; the container re-clamps as defence-in-depth.
_DEFAULT_MAX_DATA_TRIPLES = 100_000
_DEFAULT_MAX_SHAPE_TRIPLES = 50_000
_DEFAULT_MAX_ONTOLOGY_TRIPLES = 100_000
_DEFAULT_MAX_VALIDATION_DEPTH = 25
_DEFAULT_PYSHACL_TIMEOUT = 300
_DEFAULT_SPARQL_TIMEOUT = 10

_HARD_MAX_DATA_TRIPLES = 1_000_000
_HARD_MAX_SHAPE_TRIPLES = 200_000
_HARD_MAX_ONTOLOGY_TRIPLES = 500_000
_HARD_MAX_VALIDATION_DEPTH = 50
_HARD_MAX_PYSHACL_TIMEOUT = 1800
_HARD_MAX_SPARQL_TIMEOUT = 60

_VALID_TARGET_GRAPHS = {"data", "results", "union"}
_VALID_SEVERITIES = {Severity.ERROR, Severity.WARNING, Severity.INFO}


def resolve_shacl_inputs(
    *,
    validator: Validator,
    ruleset: Ruleset | None,
    submission: Submission,
) -> SHACLInputs:
    """Build the typed ``SHACLInputs`` the container needs from DB state + settings.

    Mirrors the old ``SHACLValidator._resolve_settings`` resolution: library
    ``default_ruleset`` shapes/ontology merge with step-level extras, engine knobs
    come from step metadata (falling back to the library default, then ADR
    defaults), and SPARQL-ASK assertion rows are rehydrated into typed specs.
    """
    settings = _resolve_settings(validator, ruleset)

    merged_shapes, merged_ontology, bundled = engine.merge_shapes_and_ontologies(
        default_shapes_text=settings["default_shapes_text"],
        default_ontology_text=settings["default_ontology_text"],
        default_bundled_standards=settings["default_bundled_standards"],
        step_shapes_text=settings["step_shapes_text"],
        step_ontology_text=settings["step_ontology_text"],
        step_bundled_standards=settings["step_bundled_standards"],
    )

    rdf_format = engine.detect_serialization(
        file_name=_submission_file_name(submission),
        file_type=getattr(submission, "file_type", None),
        explicit_format=settings["submission_format"],
    )

    return SHACLInputs(
        shapes_text=merged_shapes,
        ontology_text=merged_ontology,
        rdf_format=rdf_format,
        inference_mode=settings["inference_mode"],
        advanced_shacl=bool(settings["advanced_shacl"]),
        enable_advanced_features=_setting_bool(
            "SHACL_ENABLE_ADVANCED_FEATURES",
            default=False,
        ),
        submission_format=settings["submission_format"],
        shacl_result_handling=settings["shacl_result_handling"],
        bundled_standards=list(bundled),
        sparql_ask_assertions=resolve_sparql_ask_specs(validator, ruleset),
        max_data_triples=_setting_int(
            "SHACL_MAX_DATA_TRIPLES",
            _DEFAULT_MAX_DATA_TRIPLES,
            _HARD_MAX_DATA_TRIPLES,
        ),
        max_shape_triples=_setting_int(
            "SHACL_MAX_SHAPE_TRIPLES",
            _DEFAULT_MAX_SHAPE_TRIPLES,
            _HARD_MAX_SHAPE_TRIPLES,
        ),
        max_ontology_triples=_setting_int(
            "SHACL_MAX_ONTOLOGY_TRIPLES",
            _DEFAULT_MAX_ONTOLOGY_TRIPLES,
            _HARD_MAX_ONTOLOGY_TRIPLES,
        ),
        max_validation_depth=_setting_int(
            "SHACL_MAX_VALIDATION_DEPTH",
            _DEFAULT_MAX_VALIDATION_DEPTH,
            _HARD_MAX_VALIDATION_DEPTH,
        ),
        pyshacl_timeout_seconds=_setting_int(
            "SHACL_VALIDATION_TIMEOUT_SECONDS",
            _DEFAULT_PYSHACL_TIMEOUT,
            _HARD_MAX_PYSHACL_TIMEOUT,
        ),
        sparql_query_timeout_seconds=_setting_int(
            "SHACL_SPARQL_QUERY_TIMEOUT_SECONDS",
            _DEFAULT_SPARQL_TIMEOUT,
            _HARD_MAX_SPARQL_TIMEOUT,
        ),
    )


def resolve_sparql_ask_specs(
    validator: Validator,
    ruleset: Ruleset | None,
) -> list[SHACLSparqlAssertionSpec]:
    """Rehydrate SHACL ``RulesetAssertion`` rows into typed container specs.

    Merges library-validator default assertions (first) with step-level ones,
    matching the old ``_resolve_sparql_assertions`` + ``parse_sparql_assertions``
    behaviour, but emitting ``SHACLSparqlAssertionSpec`` for the envelope. Rows
    with an empty query or an invalid target/severity are skipped with a warning —
    the form already validated them at save time, so this only guards fixtures /
    imports.
    """
    specs: list[SHACLSparqlAssertionSpec] = []
    for row in _sparql_assertion_rows(validator, ruleset):
        rhs = getattr(row, "rhs", None) or {}
        if not isinstance(rhs, dict):
            logger.warning("Skipping SHACL assertion %s: rhs is not a dict", row.pk)
            continue
        query = str(rhs.get("query", "")).strip()
        target = str(rhs.get("target_graph", "data"))
        severity = str(getattr(row, "severity", Severity.ERROR))
        if not query or target not in _VALID_TARGET_GRAPHS:
            logger.warning(
                "Skipping SHACL assertion %s: empty query or bad target_graph",
                row.pk,
            )
            continue
        if severity not in _VALID_SEVERITIES:
            logger.warning(
                "Skipping SHACL assertion %s: invalid severity %s",
                row.pk,
                severity,
            )
            continue
        specs.append(
            SHACLSparqlAssertionSpec(
                target_graph=target,
                query=query,
                severity=severity,
                description=str(rhs.get("description", "") or ""),
                error_message_template=str(getattr(row, "message_template", "") or ""),
                success_message=str(getattr(row, "success_message", "") or ""),
                assertion_id=row.pk,
            ),
        )
    return specs


# =============================================================================
# Settings resolution (ported from SHACLValidator)
# =============================================================================


def _resolve_settings(
    validator: Validator,
    ruleset: Ruleset | None,
) -> dict[str, Any]:
    """Pull shapes/ontology text and engine knobs from both rulesets."""
    default_ruleset = getattr(validator, "default_ruleset", None)
    default_metadata = _safe_metadata(default_ruleset)
    step_metadata = _safe_metadata(ruleset)
    library_default_inlined = bool(step_metadata.get("library_default_inlined"))
    if library_default_inlined:
        default_shapes_text = ""
        default_ontology_text = ""
        default_bundled_standards = None
        default_metadata_for_settings: dict[str, Any] = {}
    else:
        default_shapes_text = (
            getattr(default_ruleset, "rules", "") if default_ruleset else ""
        )
        default_ontology_text = default_metadata.get("ontology_text", "") or ""
        default_bundled_standards = default_metadata.get("bundled_standards")
        default_metadata_for_settings = default_metadata

    return {
        "default_shapes_text": default_shapes_text,
        "default_ontology_text": default_ontology_text,
        "default_bundled_standards": default_bundled_standards,
        "step_shapes_text": getattr(ruleset, "rules", "") if ruleset else "",
        "step_ontology_text": step_metadata.get("ontology_text", "") or "",
        "step_bundled_standards": step_metadata.get("bundled_standards"),
        "inference_mode": _pick_setting(
            step_metadata,
            default_metadata_for_settings,
            "inference_mode",
            "rdfs",
        ),
        "advanced_shacl": _pick_setting(
            step_metadata,
            default_metadata_for_settings,
            "advanced_shacl",
            fallback=False,
        ),
        "submission_format": _pick_setting(
            step_metadata,
            default_metadata_for_settings,
            "submission_format",
            "auto",
        ),
        "shacl_result_handling": _pick_result_handling(
            step_metadata,
            default_metadata_for_settings,
        ),
    }


def _sparql_assertion_rows(validator: Validator, ruleset: Ruleset | None) -> list[Any]:
    """Merge library-validator + step-level SHACL assertion rows (defaults first)."""
    default_ruleset = getattr(validator, "default_ruleset", None)
    step_metadata = _safe_metadata(ruleset)

    merged: list[Any] = []
    if default_ruleset is not None and not step_metadata.get("library_default_inlined"):
        merged.extend(
            default_ruleset.assertions.filter(
                assertion_type=AssertionType.SHACL,
            ).order_by("order", "pk"),
        )
    if ruleset is not None:
        merged.extend(
            ruleset.assertions.filter(
                assertion_type=AssertionType.SHACL,
            ).order_by("order", "pk"),
        )
    return merged


def _safe_metadata(ruleset: Ruleset | None) -> dict[str, Any]:
    if ruleset is None:
        return {}
    meta = getattr(ruleset, "metadata", None) or {}
    if not isinstance(meta, dict):
        return {}
    return meta


def _pick_setting(
    step_metadata: dict[str, Any],
    default_metadata: dict[str, Any],
    key: str,
    fallback: Any,
) -> Any:
    """Step value wins if explicitly set; else library default; else fallback."""
    if key in step_metadata:
        return step_metadata[key]
    if key in default_metadata:
        return default_metadata[key]
    return fallback


def _pick_result_handling(
    step_metadata: dict[str, Any],
    default_metadata: dict[str, Any],
) -> str:
    """Resolve SHACL result-handling mode with a conservative fallback."""
    value = _pick_setting(
        step_metadata,
        default_metadata,
        "shacl_result_handling",
        SHACL_RESULT_HANDLING_DEFAULT,
    )
    if value in SHACL_RESULT_HANDLING_VALUES:
        return value
    return SHACL_RESULT_HANDLING_DEFAULT


def _submission_file_name(submission: Submission) -> str | None:
    return (
        getattr(submission, "original_filename", None)
        or getattr(getattr(submission, "input_file", None), "name", None)
        or getattr(submission, "filename", None)
    )


def _setting_int(name: str, default: int, hard_max: int) -> int:
    """Read a positive int Django setting, clamped to a hard maximum."""
    try:
        value = int(getattr(django_settings, name, default))
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, hard_max)


def _setting_bool(name: str, *, default: bool = False) -> bool:
    """Read a boolean Django setting with permissive string support."""
    value = getattr(django_settings, name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
