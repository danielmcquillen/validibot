"""Django-side assembly of the typed Schematron container inputs.

Mirrors ``validators/shacl/launch.py``: the container has no database, so
everything it needs is resolved here and shipped in the typed
``SchematronInputs`` (ADR-2026-07-01, D4b). Like SHACL's ``shapes_text``,
the author's Schematron rules travel **inline as text** — resolved from the
step's ``Ruleset`` (where the step-config upload stored them), falling back
to the validator's ``default_ruleset`` for library validators that bundle
rules. The container compiles the source itself (SchXslt2 transpiler
baked into the image) and runs it over the submission.

NOTE: this module imports ``validibot_shared.schematron`` (shared >= 0.12.0
for the inline-rules contract). It is intentionally NOT imported by
``config.py``/``validator.py`` (which must be importable at app boot) —
only the dispatch layer and the launch tests import it.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from typing import Any

from validibot_shared.schematron.envelopes import SchematronInputs

from validibot.validations.validators.schematron.security import (
    resolve_schematron_limits,
)

if TYPE_CHECKING:
    from validibot.validations.models import Ruleset

__all__ = [
    "SchematronRulesResolutionError",
    "resolve_schematron_inputs",
    "resolve_schematron_rules",
]


class SchematronRulesResolutionError(ValueError):
    """Raised when a step does not resolve to any Schematron rules.

    The step-config form requires rules at save time, so hitting this at
    launch means the ruleset was emptied or a fixture/import bypassed the
    form. Either way: refuse to dispatch — a run with no rules to check
    would be meaningless.
    """


def resolve_schematron_rules(
    *,
    validator: Any,
    ruleset: Ruleset | None,
) -> str:
    """Resolve the Schematron source text for a step.

    Resolution order (the SHACL precedent, minus merging — two ``.sch``
    documents cannot be concatenated the way shape graphs can):

    1. The step's own ruleset (``step.ruleset``) — where the step-config
       upload stored the author's rules.
    2. The validator's ``default_ruleset`` — library validators that bundle
       rules (e.g. an org's reusable "Our EN 16931 profile" validator).

    Raises:
        SchematronRulesResolutionError: If neither carries rules text.
    """
    step_rules = (getattr(ruleset, "rules", "") or "").strip() if ruleset else ""
    if step_rules:
        return step_rules

    default_ruleset = getattr(validator, "default_ruleset", None)
    default_rules = (
        (getattr(default_ruleset, "rules", "") or "").strip() if default_ruleset else ""
    )
    if default_rules:
        return default_rules

    msg = (
        "No Schematron rules found on the step's ruleset or the "
        "validator's default ruleset — upload rules in the step "
        "configuration before running."
    )
    raise SchematronRulesResolutionError(msg)


def resolve_schematron_inputs(
    *,
    validator: Any,
    ruleset: Ruleset | None,
) -> SchematronInputs:
    """Build the typed ``SchematronInputs`` for the container.

    The rules ship inline; the sha256 computed here is the run's
    provenance identity for the executed rules (echoed back by the
    container in ``SchematronOutputs.schematron_sha256``).

    Raises:
        SchematronRulesResolutionError: If the step resolves to no rules.
    """
    rules_text = resolve_schematron_rules(validator=validator, ruleset=ruleset)
    limits = resolve_schematron_limits()

    return SchematronInputs(
        schematron_text=rules_text,
        schematron_sha256=hashlib.sha256(rules_text.encode("utf-8")).hexdigest(),
        max_input_bytes=limits.max_input_bytes,
        max_input_depth=limits.max_input_depth,
        xslt_timeout_seconds=limits.xslt_timeout_seconds,
        max_memory_mb=limits.max_memory_mb,
        max_findings=limits.max_findings,
    )
