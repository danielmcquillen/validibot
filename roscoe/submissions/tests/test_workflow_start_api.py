import json

import pytest
from celery.exceptions import TimeoutError as CeleryTimeout
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from roscoe.users.constants import RoleCode
from roscoe.users.tests.factories import OrganizationFactory
from roscoe.users.tests.factories import UserFactory
from roscoe.users.tests.factories import grant_role
from roscoe.validations import tasks as validation_tasks
from roscoe.validations.constants import ValidationRunStatus
from roscoe.validations.models import ValidationRun
from roscoe.workflows.models import Workflow

# Try to use your existing WorkflowFactory; fall back to simple create.
try:
    from roscoe.workflows.tests.factories import (
        WorkflowFactory,  # type: ignore[attr-defined]
    )
except Exception:  # noqa: BLE001
    WorkflowFactory = None


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def org(db):
    return OrganizationFactory()


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def workflow(db, org):
    if WorkflowFactory:
        return WorkflowFactory(org=org)

    return Workflow.objects.create(org=org, name="WF 1")


def start_url(workflow) -> str:
    return f"/api/workflows/{workflow.pk}/start/"


@pytest.mark.django_db
class TestWorkflowStartAPI:
    def test_start_with_inline_json_returns_201(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        payload = {
            "content": {"hello": "world"},
            "metadata": {"source": "test"},
        }
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        body = resp.json()
        assert body["workflow"] == workflow.id
        assert body["status"] in {
            ValidationRunStatus.SUCCEEDED,
            ValidationRunStatus.FAILED,
        }
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.workflow_id == workflow.id
        assert run.submission_id is not None

    def test_start_with_inline_text_xml_returns_201(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        payload = {
            "content": "<root><v>1</v></root>",
            "filename": "sample.xml",
            "file_type": "xml",
        }
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        body = resp.json()
        assert body["workflow"] == workflow.id
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.submission is not None

    def test_start_with_file_upload_returns_201(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        file_bytes = b"Version, 9.6.0\nBuilding, Example;"
        up = SimpleUploadedFile("building.idf", file_bytes, content_type="text/plain")

        resp = api_client.post(
            start_url(workflow),
            data={
                "file": up,
                "filename": "building.idf",
                "file_type": "energyplus",
                "metadata": json.dumps({"tag": "upload"}),
            },
            format="multipart",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        body = resp.json()
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.submission
        assert run.submission.input_file

    def test_start_long_running_returns_202_and_polling_then_succeeds(  # noqa: PLR0913
        self,
        settings,
        monkeypatch,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        # Slightly increase optimistic attempts to exercise the polling loop
        settings.VALIDATION_START_ATTEMPTS = 2
        settings.VALIDATION_START_ATTEMPT_TIMEOUT = 0

        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        class FakeAsyncResult:
            id = "fake-task-id"

            def get(self, timeout=None, propagate=False):
                # Force the service path that returns 202 Accepted
                raise CeleryTimeout()

        # Bypass Celery entirely; ensure delay returns an object whose get() times out
        monkeypatch.setattr(
            validation_tasks.execute_validation_run,
            "delay",
            lambda *a, **k: FakeAsyncResult(),
        )

        payload = {"content": {"long": "run"}}
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == status.HTTP_202_ACCEPTED, resp.data
        loc = resp.headers["Location"]
        pending_id = resp.json()["id"]
        run = ValidationRun.objects.get(pk=pending_id)
        assert run.status == ValidationRunStatus.PENDING

        # Simulate background completion
        run.status = ValidationRunStatus.SUCCEEDED
        run.summary = "Completed later"
        run.completed = timezone.now()
        run.save()

        poll = api_client.get(loc)
        assert poll.status_code == status.HTTP_200_OK
        assert poll.json()["status"] == ValidationRunStatus.SUCCEEDED

    def test_requires_executor_role(self, api_client: APIClient, org, user, workflow):
        api_client.force_authenticate(user=user)
        payload = {"content": {"x": 1}}
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(payload),
            content_type="application/json",
        )
        # We return 404 to avoid leaking workflow existence
        assert resp.status_code == status.HTTP_404_NOT_FOUND
