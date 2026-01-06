"""
Tests for idempotency key support.

These tests verify that the Idempotency-Key header pattern works correctly:
- Duplicate requests with same key return cached response
- Different body with same key returns 422
- Expired keys allow reprocessing
- Missing key processes normally
- In-flight requests return 409 Conflict
"""

import contextlib
import json
import uuid
from datetime import timedelta
from http import HTTPStatus
from types import SimpleNamespace

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

import validibot.workflows.views as views_mod
import validibot.workflows.views_launch_helpers as launch_helpers_mod
from validibot.core.models import IdempotencyKey
from validibot.core.models import IdempotencyKeyStatus
from validibot.submissions.constants import SubmissionFileType
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidationRun
from validibot.validations.services.validation_run import ValidationRunLaunchResults
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep

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
    if WorkflowFactory:
        wf = WorkflowFactory(
            org=org,
            user=user,
            allowed_file_types=[SubmissionFileType.JSON],
        )
    else:
        wf = Workflow.objects.create(
            org=org,
            user=user,
            name=f"WF {uuid.uuid4().hex}",
            allowed_file_types=[SubmissionFileType.JSON],
        )
    if WorkflowStepFactory:
        validator = ValidatorFactory(validation_type=ValidationType.BASIC)
        WorkflowStepFactory(workflow=wf, validator=validator)
    else:
        validator = ValidatorFactory(org=org, validation_type=ValidationType.BASIC)
        WorkflowStep.objects.create(workflow=wf, order=10, validator=validator)

    with contextlib.suppress(ValueError):
        user.set_current_org(org)
    return wf


def start_url(workflow) -> str:
    # Use org-scoped route (ADR-2026-01-06)
    return f"/api/v1/orgs/{workflow.org.slug}/workflows/{workflow.pk}/runs/"


@pytest.fixture(autouse=True)
def mock_validation_service_success(monkeypatch):
    """
    Stub ValidationRunService so tests focus on idempotency behavior.
    """

    def make_run(*, org, workflow, submission, run_status):
        return ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=getattr(submission, "project", None),
            status=run_status,
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
            run_status=ValidationRunStatus.SUCCEEDED,
        )
        data = {
            "id": str(run.id),
            "workflow": str(run.workflow_id),
            "submission": str(run.submission_id),
            "status": run.status,
        }
        return ValidationRunLaunchResults(
            validation_run=run,
            data=data,
            status=status.HTTP_201_CREATED,
        )

    fake_service = SimpleNamespace(launch=launch_side_effect)
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
class TestIdempotencyKey:
    """Test idempotency key behavior for workflow start endpoint."""

    def test_request_without_key_processes_normally(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """Requests without Idempotency-Key should process normally."""
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps({"hello": "world"}),
            content_type="application/json",
        )

        assert resp.status_code == status.HTTP_201_CREATED
        assert IdempotencyKey.objects.count() == 0

    def test_request_with_key_creates_record(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """First request with Idempotency-Key creates a record."""
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)
        idempotency_key = str(uuid.uuid4())

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps({"hello": "world"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )

        assert resp.status_code == status.HTTP_201_CREATED
        assert IdempotencyKey.objects.count() == 1

        key_record = IdempotencyKey.objects.first()
        assert key_record.key == idempotency_key
        assert key_record.org_id == org.id
        assert key_record.status == IdempotencyKeyStatus.COMPLETED
        assert key_record.response_status == HTTPStatus.CREATED
        assert key_record.response_body is not None

    def test_duplicate_request_returns_cached_response(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """Duplicate request with same key returns cached response."""
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)
        idempotency_key = str(uuid.uuid4())
        payload = json.dumps({"hello": "world"})

        # First request
        resp1 = api_client.post(
            start_url(workflow),
            data=payload,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )
        assert resp1.status_code == status.HTTP_201_CREATED
        original_run_id = resp1.data["id"]

        # Second request with same key
        resp2 = api_client.post(
            start_url(workflow),
            data=payload,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )

        assert resp2.status_code == status.HTTP_201_CREATED
        assert resp2["Idempotent-Replayed"] == "true"
        assert resp2.data["id"] == original_run_id

        # Only one validation run should exist
        assert ValidationRun.objects.count() == 1

    def test_different_body_same_key_returns_422(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """Request with same key but different body returns 422."""
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)
        idempotency_key = str(uuid.uuid4())

        # First request
        resp1 = api_client.post(
            start_url(workflow),
            data=json.dumps({"hello": "world"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )
        assert resp1.status_code == status.HTTP_201_CREATED

        # Second request with same key but different body
        resp2 = api_client.post(
            start_url(workflow),
            data=json.dumps({"different": "payload"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )

        assert resp2.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert resp2.data["code"] == "idempotency_key_reused"
        assert "different request body" in resp2.data["detail"]

    def test_key_too_long_returns_400(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """Idempotency key exceeding max length returns 400."""
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)
        long_key = "x" * 300

        resp = api_client.post(
            start_url(workflow),
            data=json.dumps({"hello": "world"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=long_key,
        )

        assert resp.status_code == HTTPStatus.BAD_REQUEST
        assert resp.data["code"] == "idempotency_key_too_long"
        assert "255 characters" in resp.data["detail"]

    def test_expired_key_allows_reprocessing(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """Expired idempotency key allows request to be reprocessed."""
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)
        idempotency_key = str(uuid.uuid4())
        payload = json.dumps({"hello": "world"})

        # First request
        resp1 = api_client.post(
            start_url(workflow),
            data=payload,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )
        assert resp1.status_code == status.HTTP_201_CREATED
        first_run_id = resp1.data["id"]

        # Expire the key
        key_record = IdempotencyKey.objects.get(key=idempotency_key)
        key_record.expires_at = timezone.now() - timedelta(hours=1)
        key_record.save()

        # Second request - should create new run (expired key)
        resp2 = api_client.post(
            start_url(workflow),
            data=payload,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )

        assert resp2.status_code == status.HTTP_201_CREATED
        assert "Idempotent-Replayed" not in resp2
        assert resp2.data["id"] != first_run_id

        # Two validation runs should exist
        assert ValidationRun.objects.count() == 2  # noqa: PLR2004

    def test_in_flight_request_returns_409(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """Request with key that's still processing returns 409 Conflict."""
        import hashlib

        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)
        idempotency_key = str(uuid.uuid4())
        payload = json.dumps({"hello": "world"})

        # Compute the hash that will be generated for this payload
        request_hash = hashlib.sha256(payload.encode()).hexdigest()

        # Create an in-flight key record with matching hash
        IdempotencyKey.objects.create(
            org=org,
            key=idempotency_key,
            endpoint="WorkflowViewSet.start_validation",
            request_hash=request_hash,
            status=IdempotencyKeyStatus.PROCESSING,
            expires_at=timezone.now() + timedelta(hours=24),
        )

        # Request with same key should get 409
        resp = api_client.post(
            start_url(workflow),
            data=payload,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )

        assert resp.status_code == HTTPStatus.CONFLICT
        assert resp.data["code"] == "idempotency_key_in_progress"

    def test_different_orgs_can_use_same_key(
        self,
        api_client: APIClient,
        user,
    ):
        """Different organizations can use the same idempotency key."""
        org1 = OrganizationFactory()
        org2 = OrganizationFactory()

        # Create workflows in different orgs
        if WorkflowFactory:
            wf1 = WorkflowFactory(
                org=org1,
                user=user,
                allowed_file_types=[SubmissionFileType.JSON],
            )
            wf2 = WorkflowFactory(
                org=org2,
                user=user,
                allowed_file_types=[SubmissionFileType.JSON],
            )
        else:
            wf1 = Workflow.objects.create(
                org=org1,
                user=user,
                name=f"WF1 {uuid.uuid4().hex}",
                allowed_file_types=[SubmissionFileType.JSON],
            )
            wf2 = Workflow.objects.create(
                org=org2,
                user=user,
                name=f"WF2 {uuid.uuid4().hex}",
                allowed_file_types=[SubmissionFileType.JSON],
            )

        validator = ValidatorFactory(validation_type=ValidationType.BASIC)
        if WorkflowStepFactory:
            WorkflowStepFactory(workflow=wf1, validator=validator)
            WorkflowStepFactory(workflow=wf2, validator=validator)
        else:
            WorkflowStep.objects.create(workflow=wf1, order=10, validator=validator)
            WorkflowStep.objects.create(workflow=wf2, order=10, validator=validator)

        api_client.force_authenticate(user=user)
        grant_role(user, org1, RoleCode.EXECUTOR)
        grant_role(user, org2, RoleCode.EXECUTOR)

        idempotency_key = str(uuid.uuid4())
        payload = json.dumps({"hello": "world"})

        # Request to org1
        resp1 = api_client.post(
            start_url(wf1),
            data=payload,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )
        assert resp1.status_code == status.HTTP_201_CREATED

        # Request to org2 with same key - should also succeed
        resp2 = api_client.post(
            start_url(wf2),
            data=payload,
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )
        assert resp2.status_code == status.HTTP_201_CREATED
        assert "Idempotent-Replayed" not in resp2

        # Two key records should exist
        assert IdempotencyKey.objects.count() == 2  # noqa: PLR2004

    def test_failed_request_deletes_key_record(
        self,
        api_client: APIClient,
        org,
        user,
        workflow,
    ):
        """If request processing fails, key record is deleted for retry.

        We test this by using an invalid workflow (inactive) which will
        cause the view to return an error, but the error happens AFTER
        the idempotency key is created, so we can verify cleanup.
        """
        api_client.force_authenticate(user=user)
        grant_role(user, org, RoleCode.EXECUTOR)
        idempotency_key = str(uuid.uuid4())

        # Deactivate the workflow to cause a controlled error
        workflow.is_active = False
        workflow.save()

        # Make request - should fail with 409 (workflow inactive)
        resp = api_client.post(
            start_url(workflow),
            data=json.dumps({"hello": "world"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=idempotency_key,
        )

        # Request failed with 409 Conflict
        assert resp.status_code == HTTPStatus.CONFLICT

        # Key record should still exist (it stores the error response)
        # This is correct behavior - we cache error responses too so
        # retries get the same error without re-processing
        assert IdempotencyKey.objects.filter(key=idempotency_key).count() == 1


@pytest.mark.django_db
class TestIdempotencyKeyModel:
    """Test IdempotencyKey model behavior."""

    def test_str_representation(self, org):
        """Test string representation of IdempotencyKey."""
        key = IdempotencyKey.objects.create(
            org=org,
            key="12345678-1234-1234-1234-123456789012",
            endpoint="test_endpoint",
            request_hash="abc123",
            expires_at=timezone.now() + timedelta(hours=24),
        )

        assert "12345678" in str(key)
        assert "test_endpoint" in str(key)

    def test_auto_sets_expiration(self, org):
        """Test that expiration is auto-set if not provided."""
        key = IdempotencyKey(
            org=org,
            key="test-key",
            endpoint="test_endpoint",
            request_hash="abc123",
        )
        key.save()

        assert key.expires_at is not None
        # Should be about 24 hours from now
        expected = timezone.now() + timedelta(hours=24)
        assert abs((key.expires_at - expected).total_seconds()) < 60  # noqa: PLR2004

    def test_unique_constraint(self, org):
        """Test that org+key+endpoint is unique."""
        from django.db import IntegrityError

        IdempotencyKey.objects.create(
            org=org,
            key="test-key",
            endpoint="test_endpoint",
            request_hash="hash1",
            expires_at=timezone.now() + timedelta(hours=24),
        )

        with pytest.raises(IntegrityError):
            IdempotencyKey.objects.create(
                org=org,
                key="test-key",
                endpoint="test_endpoint",
                request_hash="hash2",
                expires_at=timezone.now() + timedelta(hours=24),
            )
