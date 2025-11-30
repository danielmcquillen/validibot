"""
Base classes and data structures for validator engines.

A validator engine is a class that subclasses BaseValidatorEngine and implements
the validate() method. The subclass is what does the actual validation work
in a given validation step.

You won't find any concrete implementations here; those are in other modules.
"""

from __future__ import annotations

import re
from abc import ABC
from abc import abstractmethod
from dataclasses import asdict
from dataclasses import dataclass
from gettext import gettext as _
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings

from simplevalidations.validations.cel import DEFAULT_HELPERS
from simplevalidations.validations.cel import CelHelper
from simplevalidations.validations.cel_eval import evaluate_cel_expression
from simplevalidations.validations.constants import CEL_MAX_CONTEXT_SYMBOLS
from simplevalidations.validations.constants import CEL_MAX_EVAL_TIMEOUT_MS
from simplevalidations.validations.constants import CEL_MAX_EXPRESSION_CHARS
from simplevalidations.validations.constants import CatalogEntryType
from simplevalidations.validations.constants import CatalogRunStage
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.providers import get_provider_for_validator

if TYPE_CHECKING:
    from simplevalidations.submissions.models import Submission
    from simplevalidations.validations.models import Ruleset
    from simplevalidations.validations.models import Validator


@dataclass
class ValidationIssue:
    """
    Represents a single validation problem emitted by an engine.

    Attributes:
        path: JSON Pointer / XPath / dotted path for the failing value.
        message: Human readable description of the problem.
        severity: INFO/WARNING/ERROR (default ERROR).
        code: Optional machine-readable string for grouping (e.g. "json.required").
        meta: Optional loose metadata used to enrich ValidationFinding rows.
        assertion_id: Optional RulesetAssertion PK when the issue was produced
            by a structured assertion.
    """

    path: str
    message: str
    severity: Severity = Severity.ERROR
    code: str = ""
    meta: dict[str, Any] | None = None
    assertion_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    """
    Aggregated result of a single validation step.
    passed: True when no ERROR issues were produced.
    issues: list of issues discovered (may include INFO/WARNING).
    stats: optional extra info (counts, timings, metadata).
    """

    passed: bool
    issues: list[ValidationIssue]
    workflow_step_name: str | None = None  # slug
    stats: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": [i.to_dict() for i in self.issues],
            "stats": self.stats or {},
        }


class BaseValidatorEngine(ABC):
    """
    Base class for all validator enginge implementations....the code that
    actually does the validation logic.
    Concrete subclasses should be registered in the registry keyed by ValidationType.

    To keep validator engine classes clean, we pass everything it
    needs either via the config dict or the ContentSource.
    We don't pass in any model instances.
    """

    validation_type: ValidationType
    cel_helpers = DEFAULT_HELPERS

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        # Arbitrary configuration (e.g., schema, thresholds, flags)
        self.config: dict[str, Any] = config or {}
        self.processor_name: str = self.config.get("processor_name", "").strip()

    def get_cel_helpers(self) -> dict[str, CelHelper]:
        """
        Return the helper allowlist for this engine. Subclasses can override to
        append or remove helpers based on validator metadata.
        """
        return dict(self.cel_helpers)

    def resolve_provider(self, validator: Validator):
        """
        Resolve the provider configured for the given validator, if any.
        """
        return get_provider_for_validator(validator)

    # ------------------------------------------------------------------ CEL helpers

    def _resolve_path(self, data: Any, path: str | None) -> tuple[Any, bool]:
        """
        Resolve dotted / [index] paths into nested dict/list payloads.
        Returns (value, found_flag).
        """
        if not path:
            return data, True
        current = data
        tokens = str(path or "").split(".")
        for token in tokens:
            if not token:
                continue
            if "[" in token and token.endswith("]"):
                key, index_part = token.split("[", 1)
                index_str = index_part.rstrip("]")
                if key:
                    if isinstance(current, dict) and key in current:
                        current = current[key]
                    else:
                        return None, False
                try:
                    position = int(index_str)
                except ValueError:
                    return None, False
                if isinstance(current, (list, tuple)) and 0 <= position < len(current):
                    current = current[position]
                else:
                    return None, False
            elif isinstance(current, dict) and token in current:
                current = current[token]
            else:
                return None, False
        return current, True

    def _build_cel_context(self, payload: Any, validator: Validator) -> dict[str, Any]:
        """
        Build a context mapping catalog entry slugs to values resolved from payload.
        Include the raw payload so expressions can reference it directly if needed.
        """
        context: dict[str, Any] = {"payload": payload}
        derived_enabled = getattr(settings, "ENABLE_DERIVED_SIGNALS", False)
        qs = validator.catalog_entries.all().only(
            "slug",
            "is_required",
            "entry_type",
            "run_stage",
        )
        if not derived_enabled:
            qs = qs.filter(entry_type=CatalogEntryType.SIGNAL)
        entries = list(qs)
        for entry in entries:
            value, found = self._resolve_path(payload, entry.slug)
            if found:
                if (
                    entry.entry_type == CatalogEntryType.SIGNAL
                    and entry.run_stage == CatalogRunStage.OUTPUT
                    and entry.slug in context
                ):
                    # Preserve existing input mapping; expose output via
                    # prefix for disambiguation.
                    context.setdefault(f"output.{entry.slug}", value)
                else:
                    context[entry.slug] = value
                if (
                    entry.entry_type == CatalogEntryType.SIGNAL
                    and entry.run_stage == CatalogRunStage.OUTPUT
                ):
                    context.setdefault(f"output.{entry.slug}", value)
            elif entry.is_required:
                context[entry.slug] = None

        def _collect_matches(data: Any, key: str) -> list[Any]:
            matches: list[Any] = []
            if isinstance(data, dict):
                for k, v in data.items():
                    if k == key:
                        matches.append(v)
                    matches.extend(_collect_matches(v, key))
            elif isinstance(data, list):
                for item in data:
                    matches.extend(_collect_matches(item, key))
            return matches

        if getattr(validator, "allow_custom_assertion_targets", False):
            if isinstance(payload, dict):
                for k, v in payload.items():
                    context.setdefault(k, v)
            # support lightweight partial path matches: if an identifier appears
            # exactly once anywhere in the payload tree, expose it directly.
            identifiers = set(context.keys())
            if isinstance(payload, (dict, list)):
                for key in list(payload.keys()) if isinstance(payload, dict) else []:
                    identifiers.add(key)
                for ident in identifiers:
                    matches = _collect_matches(payload, ident)
                    if len(matches) == 1 and ident not in context:
                        context[ident] = matches[0]
        return context

    def _issue_from_assertion(
        self,
        assertion,
        path: str,
        message: str,
    ) -> ValidationIssue:
        return ValidationIssue(
            path=path,
            message=message,
            severity=assertion.severity,
            code=assertion.operator,
            meta={"ruleset_id": assertion.ruleset_id},
            assertion_id=getattr(assertion, "id", None),
        )

    def run_cel_assertions_for_stages(
        self,
        *,
        validator: Validator,
        ruleset: Ruleset,
        input_payload: Any | None = None,
        output_payload: Any | None = None,
    ) -> list[ValidationIssue]:
        """
        Convenience wrapper to evaluate CEL assertions for input/output stages.

        Engines can pass whichever payloads they have available; this keeps the
        two-pass CEL pattern consistent across engines while still allowing
        subclasses to preprocess the stage-specific payloads before invoking.
        """

        if validator is None:
            raise ValueError("validator must be provided.")
        if ruleset is None:
            raise ValueError("ruleset is required for CEL evaluation.")

        if input_payload is None and output_payload is None:
            raise ValueError(
                "At least one of input_payload or output_payload must be provided."
            )

        issues: list[ValidationIssue] = []
        if input_payload is not None:
            issues.extend(
                self.evaluate_cel_assertions(
                    ruleset=ruleset,
                    validator=validator,
                    payload=input_payload,
                    target_stage="input",
                ),
            )
        if output_payload is not None:
            issues.extend(
                self.evaluate_cel_assertions(
                    ruleset=ruleset,
                    validator=validator,
                    payload=output_payload,
                    target_stage="output",
                ),
            )
        return issues

    def evaluate_cel_assertions(
        self,
        *,
        ruleset: Ruleset,
        validator: Validator,
        payload: Any,
        target_stage: str,
    ) -> list[ValidationIssue]:
        """
        Evaluate CEL assertions on the given ruleset using a context derived
        from the validator catalog and payload. Returns a list of issues.
        Only assertions targeting the given run_stage (via target_catalog_entry) are
        evaluated; assertions without a target_catalog_entry are treated as INPUT-stage.
        """

        if validator is None:
            raise ValueError("validator must be provided.")
        if ruleset is None:
            raise ValueError("ruleset is required for CEL evaluation.")

        if payload is None:
            return []
        if target_stage not in {"input", "output"}:
            return []
        assertions = list(
            ruleset.assertions.filter(assertion_type="cel_expr").order_by("order", "pk")
        )
        if not assertions:
            return []
        try:
            context = self._build_cel_context(payload, validator)
        except Exception as exc:
            return [
                ValidationIssue(
                    path="",
                    message=_("Unable to build CEL context: %(err)s") % {"err": exc},
                    severity=getattr(validator, "severity", None) or Severity.ERROR,
                ),
            ]
        issues: list[ValidationIssue] = []
        for assertion in assertions:
            if (
                assertion.target_catalog_entry
                and assertion.target_catalog_entry.run_stage != target_stage
            ):
                continue
            if not assertion.target_catalog_entry and target_stage != "input":
                continue
            expr = (assertion.rhs or {}).get("expr") or assertion.cel_cache or ""
            if len(expr) > CEL_MAX_EXPRESSION_CHARS:
                issues.append(
                    self._issue_from_assertion(
                        assertion,
                        path="",
                        message=_("CEL expression is too long."),
                    ),
                )
                continue
            if len(context) > CEL_MAX_CONTEXT_SYMBOLS:
                issues.append(
                    self._issue_from_assertion(
                        assertion,
                        path="",
                        message=_("CEL context is too large."),
                    ),
                )
                continue

            when_expr = (assertion.when_expression or "").strip()
            if when_expr:
                guard_result = evaluate_cel_expression(
                    expression=when_expr,
                    context=context,
                    timeout_ms=CEL_MAX_EVAL_TIMEOUT_MS,
                )
                if not guard_result.success:
                    issues.append(
                        self._issue_from_assertion(
                            assertion,
                            path="",
                            message=_("CEL 'when' failed: %(err)s")
                            % {"err": guard_result.error},
                        ),
                    )
                    continue
                if not guard_result.value:
                    continue

            result = evaluate_cel_expression(
                expression=expr,
                context=context,
                timeout_ms=CEL_MAX_EVAL_TIMEOUT_MS,
            )
            if not result.success:
                raw_error = str(result.error)
                msg = raw_error
                missing_ref = re.search(
                    r"undeclared reference to ['\"](?P<ident>[^'\"]+)['\"]",
                    raw_error,
                )
                identifier = None
                if missing_ref:
                    identifier = missing_ref.group("ident")
                elif "undeclared reference to" in raw_error:
                    tail = raw_error.split("undeclared reference to", 1)[1]
                    identifier = tail.strip().split()[0].strip(" '\"()\\")
                if identifier:
                    msg = _(
                        "CEL references undefined identifier '%(identifier)s'. "
                        "Ensure a matching validator catalog entry exists."
                    ) % {"identifier": identifier}
                issues.append(
                    self._issue_from_assertion(
                        assertion,
                        path="",
                        message=_("CEL evaluation failed: %(err)s") % {"err": msg},
                    ),
                )
                continue
            if not bool(result.value):
                failure_message = assertion.message_template or _(
                    "CEL assertion evaluated to false.",
                )
                issues.append(
                    self._issue_from_assertion(
                        assertion,
                        path="",
                        message=failure_message,
                    ),
                )
        return issues

    @abstractmethod
    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
    ) -> ValidationResult:
        """
        Run standard, defined validator on a submission by an API user,
        using a ruleset defined by the author.
        """
        raise NotImplementedError
