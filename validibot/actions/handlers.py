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
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.engines.registry import get as get_validator_class

if TYPE_CHECKING:
    from validibot.validations.engines.base import BaseValidatorEngine

logger = logging.getLogger(__name__)


class ValidatorStepHandler:
    """
    Adapter that wraps ValidatorEngines for the StepHandler protocol.
    """

    def execute(self, context: RunContext) -> StepResult:
        step = context.step
        run = context.validation_run
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
                        message=_(
                            "File type '%(ft)s' not supported by this validator."
                        ) % {"ft": submission.file_type},
                        severity=Severity.ERROR,
                        code="unsupported_file_type",
                    )
                ],
                stats={"file_type": submission.file_type},
            )

        # Resolve Engine
        vtype = validator.validation_type
        try:
            validator_cls = get_validator_class(vtype)
        except Exception as exc:
            logger.exception(
                "Failed to load validator engine: type=%s validator_id=%s step_id=%s",
                vtype,
                getattr(validator, "id", None),
                getattr(step, "id", None),
            )
            return StepResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=f"Failed to load validator engine '{vtype}': {exc}",
                        severity=Severity.ERROR,
                        code="engine_load_failed",
                    )
                ],
            )

        # Setup Engine
        config = getattr(step, "config", {}) or {}
        validator_engine: BaseValidatorEngine = validator_cls(config=config)
        
        # Inject Context (Backwards Compatibility)
        if getattr(validator_engine, "run_context", None) is None:
            from types import SimpleNamespace
            validator_engine.run_context = SimpleNamespace(
                validation_run=run,
                workflow_step=step,
                downstream_signals=context.downstream_signals,
            )
        else:
            validator_engine.run_context.validation_run = run
            validator_engine.run_context.workflow_step = step
            validator_engine.run_context.downstream_signals = context.downstream_signals

        # Execute
        try:
            if hasattr(validator_engine, "validate_with_run"):
                v_result = validator_engine.validate_with_run(
                    validator=validator,
                    submission=submission,
                    ruleset=getattr(step, "ruleset", None),
                    run=run,
                    step=step,
                )
            else:
                v_result = validator_engine.validate(
                    validator=validator,
                    submission=submission,
                    ruleset=getattr(step, "ruleset", None),
                )
        except Exception as exc:
            logger.exception("Validator engine execution failed")
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

    TODO: Implement actual Slack integration using slack_sdk.
    """

    def execute(self, context: RunContext) -> StepResult:
        raise NotImplementedError(
            "SlackMessageActionHandler is not yet implemented. "
            f"Step ID: {context.step.id}"
        )


class SignedCertificateActionHandler:
    """
    Handler for SignedCertificateAction.

    TODO: Implement PDF certificate generation and attachment.
    """

    def execute(self, context: RunContext) -> StepResult:
        raise NotImplementedError(
            "SignedCertificateActionHandler is not yet implemented. "
            f"Step ID: {context.step.id}"
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
