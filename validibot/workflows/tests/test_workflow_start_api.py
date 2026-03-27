"""
Tests for the workflow start REST API endpoint.

Covers the ``POST /api/v1/workflows/<uuid>/start/`` endpoint, which accepts
raw file bodies or multipart uploads and creates validation runs.  Most tests
stub ``ValidationRunService`` so we verify request parsing, authentication,
error codes, and response shaping without running actual validators.

Also tests ``_resolve_api_source()``, which reads the ``X-Validibot-Source``
header to let API clients self-identify their run source (e.g. ``MCP`` for
AI-agent-initiated runs).
"""

import contextlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

import validibot.workflows.views.launch as views_mod
import validibot.workflows.views_launch_helpers as launch_helpers_mod
from validibot.core.models import SiteSettings
from validibot.events.constants import AppEventType
from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.tracking.models import TrackingEvent
from validibot.tracking.services import TrackingEventService
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidationRun
from validibot.validations.services.validation_run import ValidationRunLaunchResults
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.constants import WorkflowStartErrorCode
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.views_launch_helpers import _resolve_api_source

try:
    from validibot.workflows.tests.factories import WorkflowFactory
    from validibot.workflows.tests.factories import WorkflowStepFactory
except Exception:
    WorkflowFactory = None
    WorkflowStepFactory = None


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
    """
    Create a test workflow with JSON, XML, and TEXT file types.

    Creates a validator with explicit supported_file_types to match the
    workflow's allowed file types. This is needed for API tests that
    submit different content types.
    """
    allowed_types = [
        SubmissionFileType.JSON,
        SubmissionFileType.XML,
        SubmissionFileType.TEXT,
    ]
    if WorkflowFactory:
        wf = WorkflowFactory(
            org=org,
            user=user,
            allowed_file_types=allowed_types,
        )
    else:
        wf = Workflow.objects.create(
            org=org,
            user=user,
            name=f"WF {uuid4().hex}",
            allowed_file_types=allowed_types,
        )
    # Create validator with explicit file type support to match workflow
    validator = ValidatorFactory(
        validation_type=ValidationType.BASIC,
        supported_file_types=allowed_types,
    )
    if WorkflowStepFactory:
        WorkflowStepFactory(workflow=wf, validator=validator)
    else:
        WorkflowStep.objects.create(workflow=wf, order=10, validator=validator)
    with contextlib.suppress(ValueError):
        user.set_current_org(org)
    return wf


@pytest.fixture
def workflow_without_steps(db, org, user):
    if WorkflowFactory:
        return WorkflowFactory(
            org=org,
            user=user,
            allowed_file_types=[SubmissionFileType.JSON],
        )
    wf = Workflow.objects.create(
        org=org,
        user=user,
        name=f"WF-no-steps-{uuid4().hex}",
        allowed_file_types=[SubmissionFileType.JSON],
    )
    with contextlib.suppress(ValueError):
        user.set_current_org(org)
    return wf


def start_url(workflow) -> str:
    """Return the org-scoped API URL for starting a workflow run."""
    return f"/api/v1/orgs/{workflow.org.slug}/workflows/{workflow.pk}/runs/"


@pytest.fixture(autouse=True)
def reset_site_settings(db):
    SiteSettings.objects.update_or_create(
        slug=SiteSettings.DEFAULT_SLUG,
        defaults={"data": {}},
    )
    yield
    SiteSettings.objects.update_or_create(
        slug=SiteSettings.DEFAULT_SLUG,
        defaults={"data": {}},
    )


@pytest.fixture(autouse=True)
def mock_validation_service_success(monkeypatch):
    """
    Default: stub the ValidationRunService used by the view so tests focus on
    parsing/routing. Creates a real ValidationRun via factory (preferred) or ORM.
    Returns 201 with minimal payload that tests assert on.
    """

    def make_run(*, org, workflow, submission, status):
        return ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=getattr(submission, "project", None),
            status=status,
        )

    def launch_side_effect(*_, **kwargs):
        request = kwargs.get("request")
        actor = getattr(request, "user", None)
        if not kwargs["workflow"].can_execute(user=actor):
            raise PermissionError("User lacks executor role")
        run = make_run(
            org=kwargs["org"],
            workflow=kwargs["workflow"],
            submission=kwargs["submission"],
            status=ValidationRunStatus.SUCCEEDED,
        )
        tracking_service = TrackingEventService()
        tracking_service.log_validation_run_created(
            run=run,
            user=actor,
            submission_id=run.submission_id,
        )
        tracking_service.log_validation_run_started(
            run=run,
            user=actor,
            extra_data={"status": ValidationRunStatus.RUNNING},
        )
        data = {
            "id": run.id,
            "workflow": run.workflow_id,
            "submission": run.submission_id,
            "status": run.status,
        }
        return ValidationRunLaunchResults(
            validation_run=run,
            data=data,
            status=status.HTTP_201_CREATED,
        )

    fake_service = SimpleNamespace(launch=launch_side_effect)
    # Patch where the view LOOKS UP the class
    monkeypatch.setattr(
        views_mod,
        "ValidationRunService",
        lambda: fake_service,
        raising=True,
    )
    monkeypatch.setattr(
        launch_helpers_mod,
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

    def test_start_with_invalid_json_returns_error(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.post(
            start_url(workflow),
            data="{invalid-json",
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.INVALID_PAYLOAD
        assert resp.data["errors"][0]["field"] == "content"
        assert "Invalid JSON payload" in resp.data["errors"][0]["message"]

    def test_start_with_unsupported_content_type_returns_error(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.post(
            start_url(workflow),
            data=b"raw-binary",
            content_type="application/pdf",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.INVALID_PAYLOAD
        assert "Unsupported Content-Type" in resp.data["errors"][0]["message"]

    def test_start_rejects_disallowed_file_type_even_with_valid_content_type(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        workflow.allowed_file_types = [SubmissionFileType.JSON]
        workflow.save(update_fields=["allowed_file_types"])
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.post(
            start_url(workflow),
            data="<root/>",
            content_type="application/xml",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.FILE_TYPE_UNSUPPORTED
        assert "accepts" in resp.data["detail"]

    def test_start_rejects_when_step_cannot_process_selected_file_type(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        workflow.allowed_file_types = [
            SubmissionFileType.JSON,
            SubmissionFileType.XML,
        ]
        workflow.save(update_fields=["allowed_file_types"])
        step = workflow.steps.first()
        step.validator = ValidatorFactory(
            validation_type=ValidationType.JSON_SCHEMA,
        )
        step.save(update_fields=["validator"])
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.post(
            start_url(workflow),
            data="<root/>",
            content_type="application/xml",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.FILE_TYPE_UNSUPPORTED
        assert "does not support" in resp.data["detail"]

    def test_start_with_missing_content_type_returns_error(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.generic(
            "POST",
            start_url(workflow),
            b"no-header",
            content_type="",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.INVALID_PAYLOAD
        assert "Missing Content-Type" in resp.data["errors"][0]["message"]

    def test_metadata_key_value_only_blocks_nested_values(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        SiteSettings.objects.update_or_create(
            slug=SiteSettings.DEFAULT_SLUG,
            defaults={
                "data": {
                    "api_submission": {
                        "metadata_key_value_only": True,
                    },
                },
            },
        )
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": "{}",
            "content_type": "application/json",
            "metadata": {"nested": {"oops": True}},
        }

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.INVALID_PAYLOAD
        assert "Metadata value for 'nested'" in resp.data["errors"][0]["message"]

    def test_metadata_size_limit_enforced(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        SiteSettings.objects.update_or_create(
            slug=SiteSettings.DEFAULT_SLUG,
            defaults={
                "data": {
                    "api_submission": {
                        "metadata_max_bytes": 20,
                    },
                },
            },
        )
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": "{}",
            "content_type": "application/json",
            "metadata": {"big": "x" * 100},
        }

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data["code"] == WorkflowStartErrorCode.INVALID_PAYLOAD
        assert "Metadata is too large" in resp.data["errors"][0]["message"]

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
        run_id = resp.data["id"]

        created_event = TrackingEvent.objects.get(
            app_event_type=AppEventType.VALIDATION_RUN_CREATED,
            extra_data__validation_run_id=run_id,
        )
        started_event = TrackingEvent.objects.get(
            app_event_type=AppEventType.VALIDATION_RUN_STARTED,
            extra_data__validation_run_id=run_id,
        )

        assert created_event.project_id == project.id
        assert created_event.org_id == org.id
        assert created_event.user_id == user.id
        assert created_event.extra_data.get("workflow_pk") == workflow.pk

        assert started_event.project_id == project.id
        assert started_event.extra_data.get("status") == ValidationRunStatus.RUNNING

    def test_start_defaults_to_workflow_project(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ) -> None:
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
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.project_id == workflow.project_id

        created_event = TrackingEvent.objects.get(
            app_event_type=AppEventType.VALIDATION_RUN_CREATED,
            extra_data__validation_run_id=body["id"],
        )
        assert created_event.project_id == workflow.project_id

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

    def test_json_envelope_accepts_object_content(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ) -> None:
        """Mode 2 should coerce dict/list content into stored text."""

        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": {"example": True},
            "content_type": "application/json",
            "filename": "body.json",
        }

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        body = resp.json()
        run = ValidationRun.objects.get(pk=body["id"])
        assert run.submission is not None
        assert run.submission.content.strip() == json.dumps(envelope["content"])

    def test_json_envelope_infers_content_type_for_json_payload(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ) -> None:
        """If content_type is omitted, JSON payloads fall back to inference."""

        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": {"hello": "world"},
            "filename": "data.json",
        }

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        run = ValidationRun.objects.get(pk=resp.data["id"])
        assert run.submission is not None
        assert run.submission.file_type == SubmissionFileType.JSON

    def test_json_envelope_infers_content_type_for_xml_payload(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ) -> None:
        """Filename and content sniffing should infer XML when not provided."""

        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        envelope = {
            "content": "<root><value>1</value></root>",
            "filename": "sample.xml",
        }

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_201_CREATED, resp.data
        run = ValidationRun.objects.get(pk=resp.data["id"])
        assert run.submission is not None
        assert run.submission.file_type == SubmissionFileType.XML

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

    def test_start_long_running_returns_202_and_polling_then_succeeds(
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
                    org=org,
                    workflow=workflow,
                    submission=submission,
                    status=status,
                )
            return ValidationRun.objects.create(
                org=org,
                workflow=workflow,
                submission=submission,
                status=status,
            )

        # Override the autouse 201 stub with a 202 stub for THIS test only
        def pending_side_effect(*_, **kwargs):
            request = kwargs.get("request")
            actor = getattr(request, "user", None)
            if not kwargs["workflow"].can_execute(user=actor):
                raise PermissionError("User lacks executor role")
            run = make_run(
                org=kwargs["org"],
                workflow=kwargs["workflow"],
                submission=kwargs["submission"],
                status=ValidationRunStatus.PENDING,
            )
            return ValidationRunLaunchResults(
                validation_run=run,
                data={"id": run.id, "status": run.status},
                status=status.HTTP_202_ACCEPTED,
            )

        fake_service = SimpleNamespace(launch=pending_side_effect)
        monkeypatch.setattr(
            views_mod,
            "ValidationRunService",
            lambda: fake_service,
            raising=True,
        )
        monkeypatch.setattr(
            launch_helpers_mod,
            "ValidationRunService",
            lambda: fake_service,
            raising=True,
        )

        # auth + role
        api_client.force_authenticate(user=user)

        grant_role(user, org, RoleCode.EXECUTOR)

        # POST -> expect 202 + Location
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps({"long": "run"}),
            content_type="application/json",
        )
        assert resp.status_code == status.HTTP_202_ACCEPTED, resp.data
        loc = resp["Location"]
        pending_id = resp.json()["id"]

        # flip run to SUCCEEDED to simulate background completion
        run = ValidationRun.objects.get(pk=pending_id)
        run.status = ValidationRunStatus.SUCCEEDED

        run.completed = timezone.now()
        run.save()

        # poll
        poll = api_client.get(loc)
        assert poll.status_code == status.HTTP_200_OK
        assert poll.json()["status"] == ValidationRunStatus.SUCCEEDED

    def test_requires_executor_role(self, api_client: APIClient, org, workflow):
        """
        Without EXECUTOR role we respond with 404 to avoid leaking existence.
        """
        viewer = UserFactory()
        grant_role(viewer, org, RoleCode.WORKFLOW_VIEWER)
        viewer.set_current_org(org)
        api_client.force_authenticate(user=viewer)
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

    def test_start_rejects_inactive_workflow(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
        mock_validation_service_success,
    ):
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)
        workflow.is_active = False
        workflow.save(update_fields=["is_active"])

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps({"content": {"example": True}}),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_409_CONFLICT
        assert resp.data == {
            "detail": "",
            "code": WorkflowStartErrorCode.WORKFLOW_INACTIVE.value,
        }

    def test_start_rejects_workflow_without_steps(
        self,
        api_client: APIClient,
        org,
        user,
        workflow_without_steps,
        mock_validation_service_success,
    ):
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.post(
            start_url(workflow_without_steps),
            data=json.dumps({"content": {"example": True}}),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.data == {
            "detail": "This workflow has no steps defined and cannot be executed.",
            "code": WorkflowStartErrorCode.NO_WORKFLOW_STEPS.value,
        }


# ── _resolve_api_source ──────────────────────────────────────────────────
# The X-Validibot-Source header lets API clients self-identify the source
# of a validation run (e.g. the MCP server sends "MCP"). The function
# validates the header against the ValidationRunSource enum and rejects
# spoofing of LAUNCH_PAGE (which is reserved for the web form).


def _fake_request(source_header: str | None = None) -> MagicMock:
    """Build a minimal Django-like request with an optional source header.

    Django 4.0+ provides ``request.headers`` — a case-insensitive dict
    that maps standard HTTP header names (e.g. ``x-validibot-source``).
    The ``_resolve_api_source()`` function uses this modern interface.
    """
    headers: dict[str, str] = {}
    if source_header is not None:
        headers["x-validibot-source"] = source_header
    request = MagicMock()
    request.headers = headers
    return request


class TestResolveApiSource:
    """Verify that _resolve_api_source correctly maps the X-Validibot-Source
    header to a ValidationRunSource enum value.

    This is a pure function with no database dependency — it only reads
    request.headers. The tests ensure the enum constraint is enforced and
    that LAUNCH_PAGE cannot be spoofed by external API callers.
    """

    def test_mcp_header_returns_mcp_source(self):
        """The MCP server sends X-Validibot-Source: MCP on every request."""
        result = _resolve_api_source(_fake_request("MCP"))
        assert result == ValidationRunSource.MCP

    def test_api_header_returns_api_source(self):
        """An explicit 'API' header should be accepted as-is."""
        result = _resolve_api_source(_fake_request("API"))
        assert result == ValidationRunSource.API

    def test_missing_header_defaults_to_api(self):
        """When no header is sent, the source should default to API.

        This preserves backward compatibility — existing API clients that
        predate the header will continue to be recorded as API-sourced.
        """
        result = _resolve_api_source(_fake_request())
        assert result == ValidationRunSource.API

    def test_empty_header_defaults_to_api(self):
        """An empty string header is treated the same as missing."""
        result = _resolve_api_source(_fake_request(""))
        assert result == ValidationRunSource.API

    def test_invalid_value_defaults_to_api(self):
        """Unrecognized header values must not crash — default to API.

        This protects against typos or future client versions sending
        values the server doesn't yet understand.
        """
        result = _resolve_api_source(_fake_request("EVIL"))
        assert result == ValidationRunSource.API

    def test_launch_page_spoofing_blocked(self):
        """LAUNCH_PAGE is reserved for the web form and must not be claimable
        by API callers.

        Without this guard, an API caller could send X-Validibot-Source:
        LAUNCH_PAGE to disguise automated runs as human-initiated, skewing
        usage analytics and potentially bypassing future rate limits.
        """
        result = _resolve_api_source(_fake_request("LAUNCH_PAGE"))
        assert result == ValidationRunSource.API

    def test_case_sensitive(self):
        """The header value must match the enum exactly — lowercase 'mcp'
        should not resolve to MCP.

        ValidationRunSource uses uppercase values. Case-insensitive matching
        would create ambiguity if new enum members were added.
        """
        result = _resolve_api_source(_fake_request("mcp"))
        assert result == ValidationRunSource.API
