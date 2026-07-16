"""
Base class for synchronous, inline validators.

All simple validators follow the same lifecycle:

1. Check file type compatibility
2. Parse submission content into a domain object
3. Run domain-specific structural and semantic checks
4. Extract step outputs for downstream assertion evaluation
5. Evaluate input-stage and output-stage assertions
6. Return a complete ValidationResult

Subclasses implement the domain-specific steps (1-4). The base class
handles assertion evaluation (5) and result assembly (6).

Simple validators complete entirely within a single validate() call.
They do not require run_context for container orchestration, though
they receive it for cross-step value access.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.constants import Severity
from validibot.validations.validators.base.base import AssertionStats
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator

logger = logging.getLogger(__name__)


class SimpleValidator(BaseValidator):
    """
    Abstract base for synchronous validators using the Template Method pattern.

    Subclasses implement four hooks that define domain-specific behavior:

    - ``validate_file_type()`` - check file compatibility
    - ``parse_content()`` - parse submission into a domain object
    - ``run_domain_checks()`` - structural and semantic validation
    - ``extract_output_values()`` - extract key-value step outputs for assertions

    The concrete ``validate()`` method calls these hooks in sequence,
    handles errors, evaluates assertions, and assembles the final
    ``ValidationResult``. Subclasses should not override ``validate()``.
    """

    @abstractmethod
    def validate_file_type(
        self,
        submission: Submission,
    ) -> ValidationIssue | None:
        """
        Check whether the submission's file type is compatible with this validator.

        Return None if the file is acceptable, or a ValidationIssue with
        severity ERROR if it is not.
        """
        ...

    @abstractmethod
    def parse_content(
        self,
        submission: Submission,
    ) -> Any:
        """
        Read and parse the submission content into a domain object.

        The return type is validator-specific: a dict for JSON-based
        validators, an lxml ElementTree for XML validators, a custom
        dataclass for domain parsers, etc.

        Should raise an exception on parse failure. The base class
        validate() method catches exceptions and converts them to
        ERROR-severity ValidationIssues.
        """
        ...

    @abstractmethod
    def run_domain_checks(
        self,
        parsed: Any,
    ) -> list[ValidationIssue]:
        """
        Run domain-specific validation checks on the parsed object.

        Return a list of issues found. An empty list means all domain
        checks passed. Issues can have any severity - ERROR issues cause
        the validation to fail; WARNING and INFO issues are reported but
        do not fail the step.
        """
        ...

    def extract_output_values(
        self,
        parsed: Any,
    ) -> dict[str, Any]:
        """
        Extract step outputs from the parsed object for assertion evaluation.

        The returned key-value pairs become:

        1. The payload for this step's assertion evaluation (input and
           output stages)
        2. Available to downstream workflow steps as
           ``steps.<step_key>.output.<contract_key>``.

        Default implementation returns an empty dict. Override when your
        validator extracts structured data that assertions or downstream
        steps should reference.
        """
        return {}

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Template method implementing the simple validation lifecycle.

        Calls the abstract methods in sequence, handles errors, evaluates
        assertions, and assembles the final ValidationResult.

        Subclasses should NOT override this method. Override the abstract
        methods instead.
        """
        self.run_context = run_context
        issues: list[ValidationIssue] = []

        # 1. File type check
        file_type_issue = self.validate_file_type(submission)
        if file_type_issue:
            return ValidationResult(passed=False, issues=[file_type_issue])

        # 2. Parse content
        try:
            parsed = self.parse_content(submission)
        except Exception as exc:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=f"Failed to parse submission: {exc}",
                        severity=Severity.ERROR,
                    ),
                ],
                stats={"parse_exception": type(exc).__name__},
            )

        # 3. Domain checks
        domain_issues = self.run_domain_checks(parsed)
        issues.extend(domain_issues)

        # 4. Extract step outputs.
        output_values = self.extract_output_values(parsed)

        # 5. Evaluate assertions (input and output stages).
        # The payload passed to evaluators is enriched with
        # namespaced values (i.* / s.* / o.*) so BASIC assertions
        # whose targets reference those namespaces resolve correctly
        # — see ``BaseValidator._enrich_basic_payload``. CEL ignores
        # ``payload`` entirely (it reads from a separately-built
        # context), so the enrichment is a no-op for CEL targets.
        total_assertions = 0
        total_failures = 0
        for stage in ("input", "output"):
            stage_output_values = output_values if stage == "output" else None
            enriched_payload = self._enrich_basic_payload(
                stage_output_values,
                stage=stage,
                output_values=stage_output_values,
            )
            result = self.evaluate_assertions_for_stage(
                validator=validator,
                ruleset=ruleset,
                payload=enriched_payload,
                stage=stage,
            )
            issues.extend(result.issues)
            total_assertions += result.total
            total_failures += result.failures

        # 6. Assemble result
        passed = not any(i.severity == Severity.ERROR for i in issues)
        return ValidationResult(
            passed=passed,
            issues=issues,
            assertion_stats=AssertionStats(
                total=total_assertions,
                failures=total_failures,
            ),
            output_values=output_values if output_values else None,
        )
