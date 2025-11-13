import pytest
from celery.exceptions import TimeoutError as CeleryTimeout
from rest_framework import status
from rest_framework.test import APIRequestFactory

from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.base import ValidationIssue
from simplevalidations.validations.engines.base import ValidationResult
from simplevalidations.validations.models import ValidationFinding
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.models import ValidationRunSummary
from simplevalidations.validations.services.validation_run import GENERIC_EXECUTION_ERROR
from simplevalidations.validations.services.validation_run import ValidationRunService
from simplevalidations.validations.tests.factories import RulesetAssertionFactory
from simplevalidations.validations.tests.factories import RulesetFactory
from simplevalidations.validations.tests.factories import ValidatorFactory
from simplevalidations.workflows.tests.factories import WorkflowFactory
from simplevalidations.workflows.tests.factories import WorkflowStepFactory


@pytest.mark.django_db
def test_launch_commits_run_before_enqueue(monkeypatch):
    org = OrganizationFactory()
    user = UserFactory()
    grant_role(user, org, RoleCode.EXECUTOR)
    workflow = WorkflowFactory(org=org, user=user, is_active=True)
    WorkflowStepFactory(workflow=workflow)  # ensure workflow has a validator step
    submission = SubmissionFactory(org=org, project=workflow.project, user=user, workflow=workflow)

    factory = APIRequestFactory()
    request = factory.post("/api/v1/workflows/start/")
    request.user = user

    recorded_run_ids: list[int] = []

    def fake_apply_async(*, kwargs=None, **_):
        run_id = kwargs["validation_run_id"]
        assert ValidationRun.objects.filter(pk=run_id).exists()
        recorded_run_ids.append(run_id)

        class DummyResult:
            def get(self, timeout=None, propagate=False):
                raise CeleryTimeout()

        return DummyResult()

    monkeypatch.setattr(
        "simplevalidations.validations.tasks.execute_validation_run.apply_async",
        fake_apply_async,
    )

    service = ValidationRunService()
    response = service.launch(
        request=request,
        org=org,
        workflow=workflow,
        submission=submission,
        user_id=user.id,
        metadata=None,
    )

    assert recorded_run_ids, "Task should be enqueued after ValidationRun commit."
    assert response.status_code in {status.HTTP_202_ACCEPTED, status.HTTP_201_CREATED}


@pytest.mark.django_db
def test_execute_sets_generic_error_when_engine_missing():
    org = OrganizationFactory()
    user = UserFactory()
    workflow = WorkflowFactory(org=org, user=user, is_active=True)
    validator = ValidatorFactory(
        org=org,
        validation_type=ValidationType.CUSTOM_RULES,
        is_system=False,
    )
    WorkflowStepFactory(workflow=workflow, validator=validator)
    submission = SubmissionFactory(org=org, project=workflow.project, user=user, workflow=workflow)
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
    assert validation_run.status == ValidationRunStatus.FAILED
    assert validation_run.error == GENERIC_EXECUTION_ERROR
    assert result.error == GENERIC_EXECUTION_ERROR


@pytest.mark.django_db
def test_execute_persists_findings_and_summary(monkeypatch):
    org = OrganizationFactory()
    user = UserFactory()
    workflow = WorkflowFactory(org=org, user=user, is_active=True)
    validator = ValidatorFactory(
        org=org,
        validation_type=ValidationType.BASIC,
        is_system=False,
    )
    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=RulesetType.BASIC,
    )
    step = WorkflowStepFactory(workflow=workflow, validator=validator, ruleset=ruleset)
    assertion = RulesetAssertionFactory(ruleset=ruleset)
    submission = SubmissionFactory(org=org, project=workflow.project, user=user, workflow=workflow)
    validation_run = ValidationRun.objects.create(
        org=org,
        workflow=workflow,
        submission=submission,
        project=submission.project,
        user=user,
        status=ValidationRunStatus.PENDING,
    )

    issue = ValidationIssue(
        path="payload.price",
        message="Price exceeds limit",
        severity=Severity.ERROR,
        assertion_id=assertion.id,
    )
    fake_result = ValidationResult(
        passed=False,
        issues=[issue],
        stats={"assertion_count": 1},
    )

    monkeypatch.setattr(
        ValidationRunService,
        "execute_workflow_step",
        lambda self, step, validation_run: fake_result,
    )

    service = ValidationRunService()
    result = service.execute(
        validation_run_id=validation_run.id,
        user_id=user.id,
        metadata=None,
    )

    validation_run.refresh_from_db()
    assert result.status == ValidationRunStatus.FAILED
    assert validation_run.status == ValidationRunStatus.FAILED
    assert validation_run.step_runs.count() == 1

    findings = ValidationFinding.objects.filter(validation_run=validation_run)
    assert findings.count() == 1
    finding = findings.first()
    assert finding.message == "Price exceeds limit"
    assert finding.ruleset_assertion_id == assertion.id
    assert finding.validation_step_run.workflow_step == step

    summary_record = ValidationRunSummary.objects.get(run=validation_run)
    assert summary_record.error_count == 1
    assert summary_record.total_findings == 1
    assert summary_record.assertion_failure_count == 1
    assert summary_record.assertion_total_count == 1
    assert summary_record.step_summaries.count() == 1
