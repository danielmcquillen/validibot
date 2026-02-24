from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext as _

from validibot.actions.constants import CertificationActionType
from validibot.actions.constants import IntegrationActionType
from validibot.actions.protocols import RunContext
from validibot.actions.protocols import StepResult
from validibot.actions.registry import register_action_handler
from validibot.validations.constants import Severity
from validibot.validations.validators.base import ValidationIssue
from validibot.validations.validators.base.registry import get as get_validator_class

if TYPE_CHECKING:
    from validibot.validations.validators.base import BaseValidator

logger = logging.getLogger(__name__)


class ValidatorStepHandler:
    """
    Adapter that bridges validators to the unified StepHandler protocol.

    This handler is the glue between the workflow engine (which speaks the
    StepHandler protocol) and the various validators (XML, JSON, Basic,
    EnergyPlus, FMU, AI). It's automatically invoked when a WorkflowStep has
    an associated Validator.

    Execution flow:
        1. Extracts the Validator from the WorkflowStep
        2. Validates file type compatibility
        3. Resolves the appropriate validator class from the registry
        4. Instantiates the validator with step-level config
        5. Calls validator.validate() with the submission and run_context
        6. Translates ValidationResult → StepResult

    For advanced validators (EnergyPlus, FMU), the validator dispatches to a
    container job and returns a pending result. The workflow engine handles the
    async completion via callbacks.

    Example:
        This handler is not called directly. The ValidationRunService
        dispatches to it when processing a validator step::

            # In ValidationRunService.execute_step():
            handler = ValidatorStepHandler()
            result = handler.execute(run_context)

    See Also:
        - BaseValidator: The abstract base class for all validators
        - StepHandler: The protocol this class implements
        - ValidationRunService: The dispatcher that invokes this handler
    """

    def execute(self, run_context: RunContext) -> StepResult:
        step = run_context.step
        run = run_context.validation_run
        validator = getattr(step, "validator", None)

        if not validator:
            logger.error(
                "WorkflowStep has no validator configured: step_id=%s run_id=%s",
                getattr(step, "id", None),
                getattr(run, "id", None),
            )
            return StepResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_("WorkflowStep has no validator configured."),
                        severity=Severity.ERROR,
                        code="missing_validator",
                    )
                ],
            )

        # File type check
        submission = run.submission
        if submission and not validator.supports_file_type(submission.file_type):
            return StepResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_("File type '%(ft)s' not supported by this validator.")
                        % {"ft": submission.file_type},
                        severity=Severity.ERROR,
                        code="unsupported_file_type",
                    )
                ],
                stats={"file_type": submission.file_type},
            )

        # Resolve validator class
        vtype = validator.validation_type
        try:
            validator_cls = get_validator_class(vtype)
        except Exception as exc:
            logger.exception(
                "Failed to load validator: type=%s validator_id=%s step_id=%s",
                vtype,
                getattr(validator, "id", None),
                getattr(step, "id", None),
            )
            return StepResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=f"Failed to load validator '{vtype}': {exc}",
                        severity=Severity.ERROR,
                        code="validator_load_failed",
                    )
                ],
            )

        # Setup validator instance
        config = getattr(step, "config", {}) or {}
        validator_instance: BaseValidator = validator_cls(config=config)

        # Execute - pass run_context as explicit argument
        try:
            v_result = validator_instance.validate(
                validator=validator,
                submission=submission,
                ruleset=getattr(step, "ruleset", None),
                run_context=run_context,
            )
        except Exception as exc:
            logger.exception("Validator execution failed")
            return StepResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=str(exc),
                        severity=Severity.ERROR,
                    )
                ],
            )

        return StepResult(
            passed=v_result.passed,
            issues=v_result.issues or [],
            stats=v_result.stats or {},
        )


class SlackMessageActionHandler:
    """
    Handler for SlackMessageAction.

    Not yet implemented. Contributions welcome — see CONTRIBUTING.md.
    """

    def execute(self, run_context: RunContext) -> StepResult:
        raise NotImplementedError(
            "SlackMessageActionHandler is not yet implemented. "
            f"Step ID: {run_context.step.id}"
        )


class SignedCertificateActionHandler:
    """
    Handler for SignedCertificateAction.

    Not yet implemented. Contributions welcome — see CONTRIBUTING.md.
    """

    def execute(self, run_context: RunContext) -> StepResult:
        raise NotImplementedError(
            "SignedCertificateActionHandler is not yet implemented. "
            f"Step ID: {run_context.step.id}"
        )


# Register Handlers
register_action_handler(
    IntegrationActionType.SLACK_MESSAGE,
    SlackMessageActionHandler,
)
register_action_handler(
    CertificationActionType.SIGNED_CERTIFICATE,
    SignedCertificateActionHandler,
)
