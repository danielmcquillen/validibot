import json
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.test import APIClient

import simplevalidations.workflows.views as views_mod
from simplevalidations.events.constants import AppEventType
from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.tracking.constants import TrackingEventType
from simplevalidations.tracking.models import TrackingEvent
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.factories import grant_role
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.tests.factories import ValidationRunFactory
from simplevalidations.workflows.models import Workflow

try:
    from simplevalidations.workflows.tests.factories import WorkflowFactory
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
def workflow(db, org, user):
    if WorkflowFactory:
        return WorkflowFactory(org=org, user=user)
    return Workflow.objects.create(org=org, name="WF 1")


def start_url(workflow) -> str:
    return f"/api/v1/workflows/{workflow.pk}/start/"


# Try to use your factory; fall back to direct create if it's not available
try:
    from simplevalidations.validations.tests.factories import ValidationRunFactory
except Exception:  # noqa: BLE001
    ValidationRunFactory = None


@pytest.fixture(autouse=True)
def mock_validation_service_success(monkeypatch):
    """
    Default: stub the ValidationRunService used by the view so tests focus on
    parsing/routing. Creates a real ValidationRun via factory (preferred) or ORM.
    Returns 201 with minimal payload that tests assert on.
    """

    def make_run(*, org, workflow, submission, status):
        if ValidationRunFactory:
            return ValidationRunFactory(
                org=org, workflow=workflow, submission=submission, status=status
            )
        return ValidationRun.objects.create(
            org=org, workflow=workflow, submission=submission, status=status
        )

    def launch_side_effect(*_, **kwargs):
        run = make_run(
            org=kwargs["org"],
            workflow=kwargs["workflow"],
            submission=kwargs["submission"],
            status=ValidationRunStatus.SUCCEEDED,
        )
        data = {
            "id": run.id,
            "workflow": run.workflow_id,
            "submission": run.submission_id,
            "status": run.status,
        }
        return Response(data, status=201)

    fake_service = SimpleNamespace(launch=launch_side_effect)
    # Patch where the view LOOKS UP the class
    monkeypatch.setattr(
        views_mod,
        "ValidationRunService",
        lambda: fake_service,
        raising=True,
    )
    return fake_service


@pytest.mark.django_db
class TestWorkflowStartAPI:
    def test_start_with_raw_body_json_returns_201(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        """
        Mode 1: raw-body JSON (no envelope). Content-Type drives interpretation.
        """
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        raw_doc = {"hello": "world"}
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(raw_doc),
            content_type="application/json",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        body = resp.json()
        assert body["workflow"] == workflow.id
        assert body["status"] in [
            ValidationRunStatus.SUCCEEDED,
            ValidationRunStatus.FAILED,
        ]
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.workflow_id == workflow.id
        assert run.submission_id is not None

    def test_start_logs_tracking_event_with_user(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ) -> None:
        project = ProjectFactory(org=org)
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": "<root><v>1</v></root>",
            "content_type": "application/xml",
            "filename": "sample.xml",
            "metadata": {"source": "test-suite"},
        }

        resp = api_client.post(
            f"{start_url(workflow)}?project={project.pk}",
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        event: TrackingEvent = TrackingEvent.objects.get()
        assert event.event_type == TrackingEventType.APP_EVENT
        assert event.app_event_type == AppEventType.VALIDATION_RUN_STARTED.value
        assert event.project_id is None  # Not supported yet
        assert event.org_id == org.id
        assert event.user_id == user.id
        assert event.extra_data.get("workflow_pk") == workflow.pk
        assert event.extra_data.get("validation_run_status") is not None

    def test_start_with_raw_body_xml_returns_201(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        """
        Mode 1: raw-body XML (no envelope). Content-Type drives interpretation.
        """
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        raw_doc = "<root><v>1</v></root>"
        resp = api_client.post(
            start_url(workflow),
            data=raw_doc,
            content_type="application/xml",
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        body = resp.json()
        assert body["workflow"] == workflow.id
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.submission is not None

    def test_start_with_envelope_xml_returns_201(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """
        Mode 2: JSON envelope with XML content.
        """
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": "<root><v>1</v></root>",
            "content_type": "application/xml",
            "filename": "sample.xml",
            "metadata": {"source": "test-suite"},
        }
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
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
        """
        Mode 3: multipart file upload. We override content_type explicitly.
        """
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        file_bytes = b"Version, 9.6.0\nBuilding, Example;"
        # Provide an IDF-like mime via override (text/x-idf handled in server mapping)
        up = SimpleUploadedFile("building.idf", file_bytes, content_type="text/x-idf")

        resp = api_client.post(
            start_url(workflow),
            data={
                "file": up,
                "filename": "building.idf",
                "content_type": "text/x-idf",
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
        """
        Force task path to time out so the endpoint returns 202,
        then simulate completion and poll.
        """

        def make_run(*, org, workflow, submission, status):
            if ValidationRunFactory:
                return ValidationRunFactory(
                    org=org, workflow=workflow, submission=submission, status=status
                )
            return ValidationRun.objects.create(
                org=org, workflow=workflow, submission=submission, status=status
            )

        # Override the autouse 201 stub with a 202 stub for THIS test only
        def pending_side_effect(*_, **kwargs):
            run = make_run(
                org=kwargs["org"],
                workflow=kwargs["workflow"],
                submission=kwargs["submission"],
                status=ValidationRunStatus.PENDING,
            )
            resp = Response({"id": run.id, "status": run.status}, status=202)
            resp["Location"] = f"/api/v1/validation-runs/{run.id}/"
            return resp

        fake_service = SimpleNamespace(launch=pending_side_effect)
        monkeypatch.setattr(
            views_mod, "ValidationRunService", lambda: fake_service, raising=True
        )

        # auth + role
        api_client.force_authenticate(user=user)

        grant_role(user, org, RoleCode.EXECUTOR)

        # POST -> expect 202 + Location
        resp = api_client.post(
            f"/api/v1/workflows/{workflow.pk}/start/",
            data=json.dumps({"long": "run"}),
            content_type="application/json",
        )
        assert resp.status_code == 202, resp.data
        loc = resp["Location"]
        pending_id = resp.json()["id"]

        # flip run to SUCCEEDED to simulate background completion
        run = ValidationRun.objects.get(pk=pending_id)
        run.status = ValidationRunStatus.SUCCEEDED

        run.completed = timezone.now()
        run.save()

        # poll
        poll = api_client.get(loc)
        assert poll.status_code == 200
        assert poll.json()["status"] == ValidationRunStatus.SUCCEEDED

    def test_requires_executor_role(self, api_client: APIClient, org, user, workflow):
        """
        Without EXECUTOR role we respond with 404 to avoid leaking existence.
        """
        api_client.force_authenticate(user=user)
        envelope = {
            "content": json.dumps({"x": 1}),
            "content_type": "application/json",
        }
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )
        # We return 404 to avoid leaking workflow existence
        assert resp.status_code == status.HTTP_404_NOT_FOUND
