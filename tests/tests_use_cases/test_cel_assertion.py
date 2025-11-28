from __future__ import annotations

import json
from pathlib import Path

import pytest

from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.users.constants import RoleCode
from simplevalidations.validations.constants import AssertionOperator
from simplevalidations.validations.constants import AssertionType
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.services.validation_run import ValidationRunService
from simplevalidations.validations.tests.factories import (
    RulesetAssertionFactory,
    RulesetFactory,
    ValidatorFactory,
)
from simplevalidations.workflows.tests.factories import WorkflowFactory, WorkflowStepFactory
from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.validations.engines.basic import BasicValidatorEngine


@pytest.mark.django_db
def test_cel_assertion_with_custom_targets_passes():
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
    step = WorkflowStepFactory(workflow=workflow, validator=validator, ruleset=ruleset)

    payload = Path("tests/assets/json/example_product.json").read_text()
    payload_data = json.loads(payload)

    # Sanity: custom targets should be added to CEL context for this validator.
    engine = BasicValidatorEngine()
    assert validator.allow_custom_assertion_targets is True
    ctx = engine._build_cel_context(payload_data, validator)
    assert "price" in ctx, ctx
    assert "rating" in ctx, ctx
    assert "tags" in ctx, ctx
    assert ctx["price"] == payload_data["price"]
    assert ctx["rating"] == payload_data["rating"]
    assert ctx["tags"] == payload_data["tags"]
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
    findings = list(validation_run.findings.values_list("message", flat=True))
    assert findings == [], f"Findings: {findings}"
    assert result.status == ValidationRunStatus.SUCCEEDED
    assert validation_run.status == ValidationRunStatus.SUCCEEDED
    assert validation_run.findings.count() == 0
    step_runs = validation_run.step_runs.all()
    assert step_runs.count() == 1
    assert step_runs.first().workflow_step == step
