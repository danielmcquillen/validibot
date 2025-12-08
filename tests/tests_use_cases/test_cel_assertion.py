from __future__ import annotations

import json
from pathlib import Path

import pytest

from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.engines.basic import BasicValidatorEngine
from validibot.validations.models import ValidationRun
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


@pytest.mark.django_db(transaction=True)
class TestCelAssertion:
    """
    Exercises CEL assertions in the BASIC validator engine when custom assertion
    targets are allowed. Ensures the validator builds a CEL context from JSON
    payloads, evaluates the assertion successfully, and records a passing
    ValidationRun with no findings.
    """

    def test_cel_assertion_with_custom_targets_passes(self):
        """
        Verify that a BASIC validator with ``allow_custom_assertion_targets`` set
        can expose payload fields directly to CEL expressions, execute the rule,
        and complete the workflow run without findings.
        """
        org = OrganizationFactory()
        user = UserFactory()
        grant_role(user, org, RoleCode.EXECUTOR)

        validator = ValidatorFactory(
            org=org,
            is_system=False,
            validation_type=ValidationType.BASIC,
            allow_custom_assertion_targets=True,
        )
        ruleset = RulesetFactory(org=org, user=user)
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": 'price > 0 && rating >= 90 && "mini" in tags'},
        )

        workflow = WorkflowFactory(org=org, user=user, is_active=True)
        step = WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
        )

        payload = Path("tests/assets/json/example_product.json").read_text()
        payload_data = json.loads(payload)

        engine = BasicValidatorEngine()
        assert validator.allow_custom_assertion_targets is True
        context = engine._build_cel_context(payload_data, validator)  # noqa: SLF001
        assert context["price"] == payload_data["price"]
        assert context["rating"] == payload_data["rating"]
        assert context["tags"] == payload_data["tags"]

        issues = engine.evaluate_cel_assertions(
            ruleset=ruleset,
            validator=validator,
            payload=payload_data,
            target_stage="input",
        )
        assert issues == []

        submission = SubmissionFactory(
            org=org,
            project=workflow.project,
            user=user,
            workflow=workflow,
            content=payload,
        )

        validation_run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=submission.project,
            user=user,
            status=ValidationRunStatus.PENDING,
        )

        service = ValidationRunService()
        result = service.execute(
            validation_run_id=validation_run.id,
            user_id=user.id,
            metadata=None,
        )

        validation_run.refresh_from_db()
        assert result.status == ValidationRunStatus.SUCCEEDED
        assert validation_run.status == ValidationRunStatus.SUCCEEDED
        assert validation_run.findings.count() == 0
        step_runs = validation_run.step_runs.all()
        assert step_runs.count() == 1
        assert step_runs.first().workflow_step == step
