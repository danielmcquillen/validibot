"""
Tests for the ValidationRun REST API viewset.

Covers the DRF ``ValidationRunViewSet`` — list, retrieve, and filter
endpoints for validation runs via the ``/api/v1/validations/`` URL
namespace.  Tests verify authentication, org-scoped filtering,
pagination, and field-level filtering via ``ValidationRunFilter``.
"""

import contextlib
import logging
from datetime import timedelta
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.test import APIRequestFactory
from rest_framework.test import force_authenticate

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import CredentialActionType
from validibot.actions.models import Action
from validibot.actions.models import ActionDefinition
from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.models import Role
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.api.viewsets import ValidationRunViewSet
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationRun
from validibot.validations.tests.factories import SignalDefinitionFactory
from validibot.validations.tests.factories import ValidationFindingFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.models import WorkflowStep
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

logger = logging.getLogger(__name__)


def _fake_pro_modules(credential):
    """Return a minimal validibot_pro module tree for community API tests."""

    pro_module = ModuleType("validibot_pro")
    credentials_module = ModuleType("validibot_pro.credentials")
    models_module = ModuleType("validibot_pro.credentials.models")
    models_module.IssuedCredential = SimpleNamespace(
        objects=SimpleNamespace(
            filter=lambda **_kwargs: SimpleNamespace(first=lambda: credential),
        ),
    )
    return {
        "validibot_pro": pro_module,
        "validibot_pro.credentials": credentials_module,
        "validibot_pro.credentials.models": models_module,
    }


def _add_signed_credential_step(workflow):
    """Attach a signed-credential action step to the given workflow."""
    definition, _ = ActionDefinition.objects.get_or_create(
        slug="signed-credential",
        defaults={
            "name": "Signed credential",
            "description": "Issue a signed credential.",
            "icon": "bi-award",
            "action_category": ActionCategoryType.CREDENTIAL,
            "type": CredentialActionType.SIGNED_CREDENTIAL,
            "required_commercial_feature": "signed_credentials",
        },
    )
    action = Action.objects.create(
        definition=definition,
        name="Signed credential",
        description="Issue a signed credential.",
    )
    return WorkflowStep.objects.create(
        workflow=workflow,
        order=10,
        action=action,
        name="Credential step",
        description="",
        notes="",
        config={},
    )


def runs_list_url(org) -> str:
    """Return org-scoped runs list URL (ADR-2026-01-06)."""
    return reverse("api:org-runs-list", kwargs={"org_slug": org.slug})


def runs_detail_url(org, run) -> str:
    """Return org-scoped runs detail URL (ADR-2026-01-06)."""
    return reverse("api:org-runs-detail", kwargs={"org_slug": org.slug, "pk": run.pk})


def runs_credential_download_url(org, run) -> str:
    """Return the org-scoped credential download URL for a run."""
    return reverse(
        "api:org-runs-credential-download",
        kwargs={"org_slug": org.slug, "pk": run.pk},
    )


class ValidationRunViewSetTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Ensure Role objects exist for every RoleCode so per-test role
        # assignments use real Role instances, not ad-hoc creations.
        Role.objects.all().delete()
        created_codes = set()
        for code in RoleCode.values:
            role, _ = Role.objects.get_or_create(
                code=code,
                defaults={
                    "name": getattr(RoleCode, code).label
                    if hasattr(RoleCode, code)
                    else code.title()
                },
            )
            created_codes.add(role.code)
        assert created_codes == set(RoleCode.values)
        assert Role.objects.count() == len(RoleCode.values)

    def setUp(self):
        self.client = APIClient()

        # Create test organization
        self.org = OrganizationFactory()

        # Create test user
        self.user = UserFactory(orgs=[self.org])  # Fixed: was orgs=[self.org]
        grant_role(self.user, self.org, RoleCode.VALIDATION_RESULTS_VIEWER)

        # Create another org and user for isolation testing
        self.other_org = OrganizationFactory()
        self.other_user = UserFactory(
            orgs=[self.other_org]
        )  # Fixed: was orgs=[self.other_org]
        grant_role(self.other_user, self.other_org, RoleCode.VALIDATION_RESULTS_VIEWER)

        # Create test project
        self.project = ProjectFactory(org=self.org)

        # Create test workflow
        self.workflow = WorkflowFactory(org=self.org, user=self.user)

        # Create test submission
        self.submission = SubmissionFactory(
            org=self.org, project=self.project, user=self.user
        )

        # Scope users to their orgs for viewset filtering
        with contextlib.suppress(ValueError):
            self.user.set_current_org(self.org)
            self.other_user.set_current_org(self.other_org)

    def test_authentication_required(self):
        """Test that authentication is required for all endpoints."""
        url = runs_list_url(self.org)
        response = self.client.get(url)
        # Returns 403 because OrgMembershipPermission denies anonymous users
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_validation_runs_default_recent_only(self):
        """Test that only recent runs (last 30 days) are returned by default."""
        self.client.force_authenticate(user=self.user)

        # Create old run (40 days ago)
        old_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )
        old_run.created = timezone.now() - timedelta(days=40)
        old_run.save()

        # Create recent run (10 days ago)
        recent_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )
        recent_run.created = timezone.now() - timedelta(days=10)
        recent_run.save()

        url = runs_list_url(self.org)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_list_validation_runs_all_flag(self):
        """Test that ?all=1 returns all runs regardless of age."""
        self.client.force_authenticate(user=self.user)

        # Create old run
        old_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )
        old_run.created = timezone.now() - timedelta(days=40)
        old_run.save()

        # Create recent run
        ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = runs_list_url(self.org)
        response = self.client.get(url, {"all": "1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)

    def test_filter_by_status(self):
        """Test filtering runs by status."""
        self.client.force_authenticate(user=self.user)

        # Create runs with different statuses
        ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )

        url = runs_list_url(self.org)
        response = self.client.get(url, {"status": ValidationRunStatus.PENDING})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(
            response.data["results"][0]["status"], ValidationRunStatus.PENDING
        )

    def test_filter_by_workflow(self):
        """Test filtering runs by workflow."""
        self.client.force_authenticate(user=self.user)

        # Create another workflow
        other_workflow = WorkflowFactory(org=self.org, user=self.user)

        # Create runs for different workflows
        ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        ValidationRunFactory(
            submission=self.submission,
            workflow=other_workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = runs_list_url(self.org)
        response = self.client.get(url, {"workflow": self.workflow.id})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        first_result = response.data["results"][0]
        self.assertEqual(first_result["workflow"], self.workflow.id)
        self.assertEqual(first_result["workflow_slug"], self.workflow.slug)

    def test_filter_by_date_range(self):
        """Test filtering runs by date range."""
        self.client.force_authenticate(user=self.user)

        # Create runs at different dates
        old_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )
        old_run.created = timezone.now() - timedelta(days=5)
        old_run.save()

        ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        # Filter for runs after 3 days ago
        after_date = (timezone.now() - timedelta(days=3)).date().isoformat()
        url = runs_list_url(self.org)
        response = self.client.get(url, {"after": after_date})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)

    def test_detail_includes_step_findings(self):
        """Detail responses should expose nested step findings for UI and CLI use."""
        self.client.force_authenticate(user=self.user)
        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.FAILED,
        )
        step_run = ValidationStepRunFactory(validation_run=run)
        finding = ValidationFindingFactory(
            validation_step_run=step_run,
            message="Too expensive",
            path="payload.price",
        )

        url = runs_detail_url(self.org, run)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        steps = response.data["steps"]
        self.assertEqual(len(steps), 1)
        issues = steps[0]["issues"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["message"], finding.message)
        self.assertEqual(issues[0]["path"], finding.path)

    def test_detail_includes_credential_metadata(self):
        """Detail responses should expose credential download metadata when present.

        Uses ``patch.dict("sys.modules", ...)`` to inject a fake Pro
        ``IssuedCredential`` model and patches ``apps.is_installed``
        directly here (rather than via the ``pro_installed`` fixture)
        because this is a TestCase subclass — pytest fixture injection
        in TestCase classes requires extra wiring. The targeted patch
        is equivalent and avoids the wiring noise.
        """
        self.client.force_authenticate(user=self.user)
        _add_signed_credential_step(self.workflow)
        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )
        issued_at = timezone.now()
        credential = SimpleNamespace(
            id=uuid4(),
            media_type="application/vc+jwt",
            created=issued_at,
        )
        with (
            patch.dict("sys.modules", _fake_pro_modules(credential)),
            patch(
                "validibot.validations.serializers.apps.is_installed",
                return_value=True,
            ),
        ):
            response = self.client.get(runs_detail_url(self.org, run))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["credential"],
            {
                "id": str(credential.id),
                "media_type": "application/vc+jwt",
                "issued_at": issued_at.isoformat(),
                "download_url": (
                    f"http://testserver{runs_credential_download_url(self.org, run)}"
                ),
            },
        )

    def test_detail_omits_credential_field_without_signed_credential_action(self):
        """
        Detail responses should omit credential metadata when the workflow
        has no credential step.
        """
        self.client.force_authenticate(user=self.user)
        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )
        credential = SimpleNamespace(
            id=uuid4(),
            media_type="application/vc+jwt",
            created=timezone.now(),
        )

        with patch.dict("sys.modules", _fake_pro_modules(credential)):
            response = self.client.get(runs_detail_url(self.org, run))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("credential", response.data)

    def test_credential_download_returns_compact_jws(self):
        """The download action should return the stored compact vc+jwt artifact."""
        self.client.force_authenticate(user=self.user)
        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )
        credential = SimpleNamespace(
            credential_jws="header.payload.signature",
            payload_json={
                "credentialSubject": {
                    "resourceLabel": "Product 1",
                },
            },
        )

        with (
            patch.dict("sys.modules", _fake_pro_modules(credential)),
            patch(
                "validibot.validations.api_views.apps.is_installed",
                return_value=True,
            ),
        ):
            response = self.client.get(runs_credential_download_url(self.org, run))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "application/vc+jwt")
        self.assertEqual(
            response["Content-Disposition"],
            (
                "attachment; filename="
                f'"product-1__{run.workflow.slug}__signed-credential.jwt"'
            ),
        )
        self.assertEqual(response.content.decode("utf-8"), "header.payload.signature")

    def test_detail_includes_state_and_result_fields(self):
        """Expose stable `state` and `result` fields for CLI/API consumers."""
        self.client.force_authenticate(user=self.user)

        pending_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )
        url = runs_detail_url(self.org, pending_run)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["state"], "PENDING")
        self.assertEqual(resp.data["result"], "UNKNOWN")

        succeeded_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )
        url = runs_detail_url(self.org, succeeded_run)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["state"], "COMPLETED")
        self.assertEqual(resp.data["result"], "PASS")

    def test_failed_run_result_uses_error_category(self):
        """Map `FAILED` runs to FAIL vs ERROR using `error_category`."""
        self.client.force_authenticate(user=self.user)

        validation_failed = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.FAILED,
            error_category=ValidationRunErrorCategory.VALIDATION_FAILED,
        )
        url = runs_detail_url(self.org, validation_failed)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["state"], "COMPLETED")
        self.assertEqual(resp.data["result"], "FAIL")

        runtime_failed = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.FAILED,
            error_category=ValidationRunErrorCategory.RUNTIME_ERROR,
        )
        url = runs_detail_url(self.org, runtime_failed)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["state"], "COMPLETED")
        self.assertEqual(resp.data["result"], "ERROR")

    def test_timed_out_run_result(self):
        """Timed out runs should return `result=TIMED_OUT`."""
        self.client.force_authenticate(user=self.user)
        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.TIMED_OUT,
        )
        url = runs_detail_url(self.org, run)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["state"], "COMPLETED")
        self.assertEqual(resp.data["result"], "TIMED_OUT")

    def test_executor_cannot_retrieve_other_users_run(self):
        """Executor scoped to org cannot fetch runs they didn't launch."""
        owner = UserFactory(orgs=[self.org])
        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            user=owner,
        )
        executor = UserFactory(orgs=[self.org])
        Membership.objects.filter(user=executor, org=self.org).update(is_active=True)
        membership = executor.memberships.get(org=self.org)
        membership.set_roles({RoleCode.EXECUTOR})

        # Use session-based auth to ensure request.user resolves to this executor.
        self.client.force_login(executor)
        url = runs_detail_url(self.org, run)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_results_viewer_can_retrieve_any_run(self):
        """VALIDATION_RESULTS_VIEWER can fetch any run in their org."""
        owner = UserFactory(orgs=[self.org])
        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            user=owner,
        )
        reviewer = UserFactory(orgs=[self.org])
        reviewer_membership = reviewer.memberships.get(org=self.org)
        reviewer_membership.set_roles({RoleCode.VALIDATION_RESULTS_VIEWER})
        reviewer.set_current_org(self.org)

        self.client.force_authenticate(user=reviewer)
        url = runs_detail_url(self.org, run)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], str(run.id))

    def test_organization_isolation(self):
        """Test that users only see runs from their own organization."""
        # Create run for user's org
        user_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        # Create project and workflow for other org
        other_project = ProjectFactory(org=self.other_org)
        other_workflow = WorkflowFactory(org=self.other_org, user=self.other_user)
        other_submission = SubmissionFactory(
            org=self.other_org, project=other_project, user=self.other_user
        )

        # Create run for other org
        other_run = ValidationRunFactory(
            submission=other_submission,
            workflow=other_workflow,
            org=self.other_org,
            project=other_project,
            status=ValidationRunStatus.PENDING,
        )

        # Test first user sees only their org's runs
        self.client.force_authenticate(user=self.user)
        url = runs_list_url(self.org)
        response = self.client.get(url, {"all": "1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], str(user_run.id))

        # Test second user sees only their org's runs
        self.client.force_authenticate(user=self.other_user)
        other_url = runs_list_url(self.other_org)
        response = self.client.get(other_url, {"all": "1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], str(other_run.id))

    def test_results_viewer_sees_all_runs_in_org(self):
        """VALIDATION_RESULTS_VIEWER can see all runs for their org."""
        reviewer = UserFactory(orgs=[self.org])
        reviewer_membership = reviewer.memberships.get(org=self.org)
        reviewer_membership.set_roles({RoleCode.VALIDATION_RESULTS_VIEWER})
        reviewer.set_current_org(self.org)
        self.client.force_authenticate(user=reviewer)

        run1 = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            user=self.user,
        )
        other_user = UserFactory(orgs=[self.org])
        other_submission = SubmissionFactory(
            org=self.org, project=self.project, user=other_user
        )
        run2 = ValidationRunFactory(
            submission=other_submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            user=other_user,
        )

        url = runs_list_url(self.org)
        response = self.client.get(url, {"all": "1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = {item["id"] for item in response.data["results"]}
        self.assertEqual(ids, {str(run1.id), str(run2.id)})

    def test_executor_sees_only_their_own_runs(self):
        """EXECUTOR without results rights only sees runs they launched."""
        # Ensure the org already has an owner so the executor isn't promoted.
        owner = UserFactory()
        owner_membership = Membership.objects.create(
            user=owner,
            org=self.org,
            is_active=True,
        )
        owner_membership.set_roles({RoleCode.OWNER})

        executor = UserFactory(orgs=[self.org])
        membership = executor.memberships.get(org=self.org)
        executor.set_current_org(self.org)
        membership.set_roles({RoleCode.EXECUTOR})
        self.assertEqual(
            set(membership.membership_roles.values_list("role__code", flat=True)),
            {RoleCode.EXECUTOR},
        )
        logger.info(
            f"target org {self.org.id} roles before request {membership.role_codes}"
        )
        own_submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=executor,
            workflow=self.workflow,
        )
        own_run = ValidationRunFactory(
            submission=own_submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            user=executor,
        )
        other_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            user=self.user,
        )
        self.assertEqual(other_run.user_id, self.user.id)
        self.assertEqual(own_run.user_id, executor.id)
        self.assertEqual(executor.current_org_id, self.org.id)

        factory = APIRequestFactory()
        request = factory.get(runs_list_url(self.org), {"all": "1"})
        force_authenticate(request, user=executor)
        with patch.object(
            Membership,
            "set_roles",
            wraps=Membership.set_roles,
        ):
            response = ValidationRunViewSet.as_view({"get": "list"})(request)
            response.render()
        membership.refresh_from_db()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        ids = {item["id"] for item in results}
        user_ids = {item.get("user") for item in results}

        self.assertIn(str(own_run.id), ids)
        self.assertNotIn(str(other_run.id), ids)
        self.assertEqual(user_ids, {executor.id})

    def test_retrieve_validation_run(self):
        """Test retrieving a specific validation run."""
        self.client.force_authenticate(user=self.user)

        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = runs_detail_url(self.org, run)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], str(run.id))
        self.assertEqual(response.data["status"], ValidationRunStatus.PENDING)

    def test_delete_validation_run(self):
        """DELETE should be rejected without removing the addressed run.

        The viewset does not support run deletion via the API, so the response
        must be ``405 METHOD NOT ALLOWED`` and the targeted run must remain in
        the database. The test avoids hard-coding the total row count because
        shared setup may legitimately create additional runs in the future.
        """
        self.client.force_authenticate(user=self.user)

        run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = runs_detail_url(self.org, run)
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertTrue(ValidationRun.objects.filter(pk=run.pk).exists())

    def test_ordering(self):
        """Test that results are ordered by creation date (newest first)."""
        self.client.force_authenticate(user=self.user)

        # Create runs with different timestamps
        old_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.SUCCEEDED,
        )
        old_run.created = timezone.now() - timedelta(hours=1)
        old_run.save()

        new_run = ValidationRunFactory(
            submission=self.submission,
            workflow=self.workflow,
            org=self.org,
            project=self.project,
            status=ValidationRunStatus.PENDING,
        )

        url = runs_list_url(self.org)
        response = self.client.get(url, {"all": "1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)

        # Newest should be first
        self.assertEqual(response.data["results"][0]["id"], str(new_run.id))
        self.assertEqual(response.data["results"][1]["id"], str(old_run.id))

    def test_create_validation_run_disallowed(self):
        """POST on validationrun-list should be disallowed (read-only viewset)."""
        self.client.force_authenticate(user=self.user)
        url = runs_list_url(self.org)
        response = self.client.post(
            url,
            {
                "submission": getattr(self, "submission", None) and self.submission.id,
                "workflow": getattr(self, "workflow", None) and self.workflow.id,
                "org": getattr(self, "org", None) and self.org.id,
                "project": getattr(self, "project", None) and self.project.id,
                "status": getattr(self, "ValidationRunStatus", None) or None,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    # ─────────────────────────────────────────────────────────────────
    # Query-count guards — [review-#5]
    # ─────────────────────────────────────────────────────────────────
    #
    # The serializer walks ``step_runs → findings`` per row and does a
    # per-row Pro credential lookup. Without the prefetch + short-circuit
    # applied in ``[review-#5]``, listing N runs issues ~O(N) extra
    # queries against the paginated window — an attacker paging the
    # feed can trivially blow up DB load. These tests pin a constant
    # ceiling so any future regression that reintroduces an N+1 fails
    # CI instead of drifting silently into production.

    def test_run_list_query_count_does_not_scale_with_run_count(self):
        """Listing more runs must not issue proportionally more queries.

        Creates a 1-run baseline and then a 5-run dataset, each with
        their own step runs + findings, and asserts the query count
        for the 5-run list is equal to the 1-run baseline — not
        5× larger as it would be without the prefetch.

        If this trips, either the Prefetch was dropped from the
        viewset queryset or the serializer started doing a per-row
        query that bypasses the prefetch cache.
        """

        def _make_run_with_steps():
            run = ValidationRunFactory(
                org=self.org,
                user=self.user,
                workflow=self.workflow,
                submission=self.submission,
                status=ValidationRunStatus.SUCCEEDED,
            )
            for _ in range(3):
                step_run = ValidationStepRunFactory(validation_run=run)
                ValidationFindingFactory(
                    validation_run=run,
                    validation_step_run=step_run,
                )
            return run

        self.client.force_authenticate(user=self.user)
        # Warm the client — first request may issue schema / system
        # queries we don't care to measure.
        self.client.get(runs_list_url(self.org))

        # One-run baseline.
        _make_run_with_steps()
        with self.assertNumQueries(17):
            response = self.client.get(runs_list_url(self.org))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data["results"]), 1)

        # Five-run count — same query count as 1 run. If this grows,
        # an N+1 has been reintroduced.
        for _ in range(4):
            _make_run_with_steps()
        with self.assertNumQueries(17):
            response = self.client.get(runs_list_url(self.org))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data["results"]), 5)

    def test_run_list_query_count_stable_with_signal_bearing_outputs(self):
        """Runs whose step_runs have populated ``output["signals"]``
        must not issue proportionally more queries than bare runs.

        This is the sneaky N+1 case the original ``[review-#5]`` fix
        missed: ``ValidationRunSerializer.get_steps`` calls
        ``build_display_signals(step_run)``, which iterates
        ``workflow_step.signal_definitions.filter(contract_key__in=...)``
        per step_run. Without a prefetch on signal_definitions (and a
        corresponding in-Python filter inside ``_build_signal_map``),
        a run with 10 step_runs × 2 signals issues ~10 extra queries
        against signal_definitions on top of the base queryset.

        The test constructs a realistic shape — each step produces
        ``signals.eui`` and ``signals.total_cost`` as output signals
        and has matching ``SignalDefinition`` rows declared on the
        validator — and compares 1-run vs 5-run query counts. If the
        N+1 ever regresses, the 5-run count grows by ~4 × the per-run
        overhead and this test fails loudly.
        """
        from validibot.validations.constants import SignalDirection

        # Build a workflow step with two OUTPUT signal definitions
        # on its validator. The serializer's
        # ``build_display_signals`` helper looks these up by
        # ``contract_key`` to enrich the dashboard payload — the
        # exact code path that used to fan out per step_run.
        target_validator = ValidatorFactory(org=self.org)
        SignalDefinitionFactory(
            validator=target_validator,
            contract_key="eui",
            direction=SignalDirection.OUTPUT,
        )
        SignalDefinitionFactory(
            validator=target_validator,
            contract_key="total_cost",
            direction=SignalDirection.OUTPUT,
        )
        target_workflow_step = WorkflowStepFactory(
            workflow=self.workflow,
            validator=target_validator,
        )

        def _make_run_with_signal_bearing_step():
            run = ValidationRunFactory(
                org=self.org,
                user=self.user,
                workflow=self.workflow,
                submission=self.submission,
                status=ValidationRunStatus.SUCCEEDED,
            )
            # One step_run per run (uq_step_run_run_step constraint
            # is per-run, so multiple step_runs would need
            # multiple workflow_steps — simpler to keep the N
            # varying on run count and pin the per-step shape
            # here).
            ValidationStepRunFactory(
                validation_run=run,
                workflow_step=target_workflow_step,
                output={
                    "signals": {
                        "eui": 42.0,
                        "total_cost": 1234.56,
                    },
                },
            )
            return run

        self.client.force_authenticate(user=self.user)
        self.client.get(runs_list_url(self.org))  # warm

        _make_run_with_signal_bearing_step()
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as one_run_ctx:
            response = self.client.get(runs_list_url(self.org))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data["results"]), 1)
            # Sanity: the output_signals enrichment actually ran.
            self.assertEqual(
                len(response.data["results"][0]["steps"][0]["output_signals"]),
                2,
            )

        one_run_query_count = len(one_run_ctx.captured_queries)

        # Add four more runs. The delta MUST be bounded by a small
        # constant — not proportional to the new run count.
        for _ in range(4):
            _make_run_with_signal_bearing_step()

        with CaptureQueriesContext(connection) as five_run_ctx:
            response = self.client.get(runs_list_url(self.org))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data["results"]), 5)

        # The real assertion — flat line between 1 and 5 runs.
        # If it regresses, signal_definitions is being queried per
        # step_run inside ``_build_signal_map``.
        self.assertEqual(
            len(five_run_ctx.captured_queries),
            one_run_query_count,
            f"signal-bearing N+1 regressed — 1-run={one_run_query_count}, "
            f"5-run={len(five_run_ctx.captured_queries)}",
        )

    def test_run_list_query_count_stable_with_many_step_runs(self):
        """Adding more step_runs to a single run must not issue
        proportionally more queries.

        Separate from the run-count test because step_runs are a
        second-level nested N+1: ``get_steps`` loops step_runs, and
        inside that loop it used to call ``step_run.findings.all()``
        which was a fresh query per step. The
        ``Prefetch(step_runs, ...).prefetch_related(findings)`` shape
        is what pins that down — this test will fail if the nested
        prefetch ever regresses to a plain ``step_runs.all()``.
        """
        run = ValidationRunFactory(
            org=self.org,
            user=self.user,
            workflow=self.workflow,
            submission=self.submission,
            status=ValidationRunStatus.SUCCEEDED,
        )
        # Ten steps with three findings each — a fat run.
        for _ in range(10):
            step_run = ValidationStepRunFactory(validation_run=run)
            for _ in range(3):
                ValidationFindingFactory(
                    validation_run=run,
                    validation_step_run=step_run,
                )

        self.client.force_authenticate(user=self.user)
        self.client.get(runs_list_url(self.org))  # warm

        with self.assertNumQueries(17):
            response = self.client.get(runs_list_url(self.org))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data["results"]), 1)

    def test_credential_short_circuit_skips_pro_query_for_non_credential_workflows(
        self,
    ):
        """Listing runs whose workflows have no signed-credential
        action must not trigger an ``IssuedCredential`` query per row.

        The short-circuit in ``get_credential`` (see refactor-step
        item ``[review-#5]``) checks
        ``workflow.has_signed_credential_action`` first and returns
        ``None`` early. Without it, every list request hits
        ``IssuedCredential.objects.filter(workflow_run=obj)`` per row
        — even when the credential field will later be popped out
        of the response entirely by ``to_representation``.

        The setUp workflow has no credential step, so this test
        verifies the short-circuit by (a) query-count stability and
        (b) asserting ``credential`` is absent from the response
        payload.
        """
        for _ in range(3):
            ValidationRunFactory(
                org=self.org,
                user=self.user,
                workflow=self.workflow,
                submission=self.submission,
                status=ValidationRunStatus.SUCCEEDED,
            )

        self.client.force_authenticate(user=self.user)
        self.client.get(runs_list_url(self.org))  # warm

        # Runs with no step_runs — the findings prefetch short-circuits
        # on empty step_runs, so we see one fewer query than the
        # other tests that set up step runs.
        with self.assertNumQueries(14):
            response = self.client.get(runs_list_url(self.org))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(len(response.data["results"]), 3)

        # ``credential`` must not be present — ``to_representation``
        # pops it for non-credential workflows. Pinning absence here
        # guards against a future change that silently starts
        # returning ``None`` under the same key.
        for row in response.data["results"]:
            self.assertNotIn("credential", row)
