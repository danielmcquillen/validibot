import pytest
from celery.exceptions import TimeoutError as CeleryTimeout
from rest_framework import status
from rest_framework.test import APIRequestFactory

from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.services.validation_run import GENERIC_EXECUTION_ERROR
from simplevalidations.validations.services.validation_run import ValidationRunService
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
