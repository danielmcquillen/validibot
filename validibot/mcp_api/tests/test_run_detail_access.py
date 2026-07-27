"""Authorization tests for authenticated MCP validation-run polling.

MCP run detail is an alternate read surface for the same ValidationRun rows
served by the web and REST APIs. These tests ensure it uses the canonical
``ValidationRunQuerySet.for_user`` policy instead of treating organization
membership alone as permission to read every result.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.mcp_api.refs import build_member_run_ref
from validibot.users.constants import RoleCode
from validibot.users.services.api_keys import issue_api_key
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.models import OrgGuestAccess
from validibot.workflows.models import WorkflowAccessGrant
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db

MCP_SERVICE_KEY = "test-mcp-run-detail-service-key"


def _run_detail_response(client, *, user, run):
    """Call the real MCP helper route using its trusted service headers."""

    issued_key = issue_api_key(user=user)
    run_ref = build_member_run_ref(
        org_slug=run.org.slug,
        run_id=str(run.pk),
    )
    return client.get(
        reverse("api:mcp:run-detail", kwargs={"run_ref": run_ref}),
        HTTP_X_MCP_SERVICE_KEY=MCP_SERVICE_KEY,
        HTTP_X_VALIDIBOT_API_TOKEN=issued_key.full_key,
    )


class TestMCPRunDetailAccess:
    """Pin role, ownership, and guest boundaries on MCP run polling."""

    @pytest.fixture(autouse=True)
    def _configure_mcp_service_key(self, settings):
        """Use the local shared-secret branch of service authentication."""

        settings.MCP_SERVICE_KEY = MCP_SERVICE_KEY

    def test_executor_can_poll_own_run(self, client):
        """An own-results role must be able to poll the run it launched."""

        org = OrganizationFactory()
        executor = UserFactory(orgs=[org])
        grant_role(executor, org, RoleCode.EXECUTOR)
        workflow = WorkflowFactory(org=org)
        run = ValidationRunFactory(
            workflow=workflow,
            org=org,
            project=workflow.project,
            user=executor,
            submission__workflow=workflow,
            submission__org=org,
            submission__project=workflow.project,
            submission__user=executor,
        )

        response = _run_detail_response(client, user=executor, run=run)

        assert response.status_code == HTTPStatus.OK
        assert response.json()["id"] == str(run.pk)

    def test_executor_cannot_poll_another_users_run(self, client):
        """Organization membership must not bypass VIEW_OWN row filtering."""

        org = OrganizationFactory()
        executor = UserFactory(orgs=[org])
        other_user = UserFactory(orgs=[org])
        grant_role(executor, org, RoleCode.EXECUTOR)
        workflow = WorkflowFactory(org=org)
        run = ValidationRunFactory(
            workflow=workflow,
            org=org,
            project=workflow.project,
            user=other_user,
            submission__workflow=workflow,
            submission__org=org,
            submission__project=workflow.project,
            submission__user=other_user,
        )

        response = _run_detail_response(client, user=executor, run=run)

        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_results_viewer_can_poll_another_users_run(self, client):
        """VIEW_ALL roles retain organization-wide result visibility."""

        org = OrganizationFactory()
        reviewer = UserFactory(orgs=[org])
        other_user = UserFactory(orgs=[org])
        grant_role(reviewer, org, RoleCode.VALIDATION_RESULTS_VIEWER)
        workflow = WorkflowFactory(org=org)
        run = ValidationRunFactory(
            workflow=workflow,
            org=org,
            project=workflow.project,
            user=other_user,
            submission__workflow=workflow,
            submission__org=org,
            submission__project=workflow.project,
            submission__user=other_user,
        )

        response = _run_detail_response(client, user=reviewer, run=run)

        assert response.status_code == HTTPStatus.OK

    def test_unrelated_org_user_cannot_poll_run(self, client):
        """An authenticated user from another tenant must receive not found."""

        workflow = WorkflowFactory()
        owner = workflow.user
        run = ValidationRunFactory(
            workflow=workflow,
            org=workflow.org,
            project=workflow.project,
            user=owner,
            submission__workflow=workflow,
            submission__org=workflow.org,
            submission__project=workflow.project,
            submission__user=owner,
        )
        outsider = UserFactory()
        outsider.memberships.all().delete()

        response = _run_detail_response(client, user=outsider, run=run)

        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_workflow_guest_can_poll_their_own_run(self, client):
        """A selected-workflow grant authorizes only the guest's own polling."""

        workflow = WorkflowFactory()
        guest = UserFactory()
        guest.memberships.all().delete()
        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=guest,
            is_active=True,
        )
        run = ValidationRunFactory(
            workflow=workflow,
            org=workflow.org,
            project=workflow.project,
            user=guest,
            submission__workflow=workflow,
            submission__org=workflow.org,
            submission__project=workflow.project,
            submission__user=guest,
        )

        response = _run_detail_response(client, user=guest, run=run)

        assert response.status_code == HTTPStatus.OK

    def test_org_guest_can_poll_only_their_own_run(self, client):
        """Org-wide guest access authorizes polling without exposing peer runs."""

        org = OrganizationFactory()
        guest = UserFactory()
        guest.memberships.all().delete()
        other_user = UserFactory(orgs=[org])
        OrgGuestAccess.objects.create(user=guest, org=org, is_active=True)
        workflow = WorkflowFactory(org=org)
        own_run = ValidationRunFactory(
            workflow=workflow,
            org=org,
            project=workflow.project,
            user=guest,
            submission__workflow=workflow,
            submission__org=org,
            submission__project=workflow.project,
            submission__user=guest,
        )
        other_run = ValidationRunFactory(
            workflow=workflow,
            org=org,
            project=workflow.project,
            user=other_user,
            submission__workflow=workflow,
            submission__org=org,
            submission__project=workflow.project,
            submission__user=other_user,
        )

        own_response = _run_detail_response(client, user=guest, run=own_run)
        other_response = _run_detail_response(client, user=guest, run=other_run)

        assert own_response.status_code == HTTPStatus.OK
        assert other_response.status_code == HTTPStatus.NOT_FOUND

    def test_guest_revocation_closes_run_polling(self, client):
        """Revoking workflow visibility must immediately close own-run polling."""

        org = OrganizationFactory()
        guest = UserFactory()
        guest.memberships.all().delete()
        access = OrgGuestAccess.objects.create(
            user=guest,
            org=org,
            is_active=True,
        )
        workflow = WorkflowFactory(org=org)
        run = ValidationRunFactory(
            workflow=workflow,
            org=org,
            project=workflow.project,
            user=guest,
            submission__workflow=workflow,
            submission__org=org,
            submission__project=workflow.project,
            submission__user=guest,
        )

        before_revoke = _run_detail_response(client, user=guest, run=run)
        access.revoke()
        after_revoke = _run_detail_response(client, user=guest, run=run)

        assert before_revoke.status_code == HTTPStatus.OK
        assert after_revoke.status_code == HTTPStatus.NOT_FOUND
