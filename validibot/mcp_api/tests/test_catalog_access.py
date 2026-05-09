"""Tests for the MCP catalog access logic.

The MCP catalog is the surface guests use to discover and run
workflows via OAuth/MCP. The access decision must compose with
``Workflow.objects.for_user`` so that:

* Family-scoped grants work — a grant on v1 of a workflow surfaces
  the latest v2 row (regression: the previous re-implementation used
  the per-row ``access_grants`` join, losing the ``(org_id, slug)``
  family scope).
* Org-wide guest access (``OrgGuestAccess``) is honoured.
* Public workflows are visible.
* The org-level ``agent_access_enabled`` master switch ONLY filters
  the member branch — grants and OrgGuestAccess override it (they
  represent deliberate cross-org exceptions).
* Cross-org ``agent_public_discovery`` workflows are visible to every
  authenticated MCP user.

Pinning these behaviours so the catalog stays in sync with
``for_user`` and so any future fix to access paths automatically
propagates.
"""

from __future__ import annotations

import pytest

from validibot.mcp_api.views import _latest_accessible_workflow_queryset
from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.models import OrgGuestAccess
from validibot.workflows.models import WorkflowAccessGrant
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _visible_pks(user):
    return set(
        _latest_accessible_workflow_queryset(user=user).values_list(
            "pk",
            flat=True,
        ),
    )


class TestMCPCatalogFamilyScopedGrant:
    """A grant on any version in a family makes the latest version visible.

    The previous implementation joined through ``access_grants`` on
    the *current* row, so a grant pinned to v1 didn't surface the
    latest v2 row in the MCP catalog. Family scoping is implemented
    by ``WorkflowQuerySet.for_user``, and the catalog now delegates
    to that — these tests pin the resulting behaviour.
    """

    def test_grant_on_v1_surfaces_latest_version_in_catalog(self):
        """A guest with a v1 grant sees v2 in the MCP catalog."""

        org = OrganizationFactory()
        author = UserFactory(orgs=[org])
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        # v1 has a grant; v2 is the latest version of the same family.
        v1 = WorkflowFactory(
            org=org,
            user=author,
            slug="my-workflow",
            version="1",
        )
        v2 = WorkflowFactory(
            org=org,
            user=author,
            slug="my-workflow",
            version="2",
        )
        WorkflowAccessGrant.objects.create(
            workflow=v1,
            user=guest,
            is_active=True,
        )

        visible = _visible_pks(guest)
        # The latest version (v2) MUST be visible — that's the family-
        # scoped behaviour. The catalog filters to "latest per family"
        # so v1 itself wouldn't be in the result anyway.
        assert v2.pk in visible


class TestMCPCatalogOrgGuestAccess:
    """OrgGuestAccess gives the catalog visibility into all org workflows."""

    def test_org_guest_access_makes_org_workflows_visible(self):
        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        OrgGuestAccess.objects.create(user=guest, org=org, is_active=True)

        wf1 = WorkflowFactory(org=org)
        wf2 = WorkflowFactory(org=org)

        visible = _visible_pks(guest)
        assert wf1.pk in visible
        assert wf2.pk in visible

    def test_org_guest_access_does_not_require_agent_access_enabled(self):
        """OrgGuestAccess overrides the org-level MCP master switch.

        ``agent_access_enabled=False`` blocks the member branch from
        the catalog, but explicit guest authorisation supersedes that
        gate by design — the operator already opted this guest in
        deliberately.
        """

        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        OrgGuestAccess.objects.create(user=guest, org=org, is_active=True)

        wf = WorkflowFactory(org=org, agent_access_enabled=False)

        visible = _visible_pks(guest)
        assert wf.pk in visible


class TestMCPCatalogMemberAgentGate:
    """Member-only workflows require ``agent_access_enabled=True``.

    The org-level master switch gates the MEMBER branch only —
    workflows reachable via grant / org-guest / public are
    unaffected. These tests pin both halves: the gate fires for
    member-only access, and other paths bypass it.
    """

    def test_member_workflow_with_agent_access_enabled_is_visible(self):
        org = OrganizationFactory()
        member = UserFactory(orgs=[org])
        # ``for_user`` requires the membership to hold a role that
        # grants ``WORKFLOW_VIEW``; the AUTHOR role does.
        grant_role(member, org, RoleCode.AUTHOR)
        wf = WorkflowFactory(org=org, agent_access_enabled=True)

        visible = _visible_pks(member)
        assert wf.pk in visible

    def test_member_only_workflow_without_agent_access_is_excluded(self):
        """Pure member access + agent_access_enabled=False → hidden.

        No grant, no OrgGuestAccess, not public, not
        agent_public_discovery — the only access path is membership,
        and the org has the MCP master switch off.
        """

        org = OrganizationFactory()
        member = UserFactory(orgs=[org])
        grant_role(member, org, RoleCode.AUTHOR)
        wf = WorkflowFactory(
            org=org,
            agent_access_enabled=False,
            is_public=False,
            agent_public_discovery=False,
        )

        visible = _visible_pks(member)
        assert wf.pk not in visible

    def test_member_with_grant_overrides_agent_access_disabled(self):
        """Grant overrides the org-level MCP master switch.

        A user with both membership AND a grant should see the
        workflow even when agent_access_enabled=False — the grant is
        the deliberate cross-cutting exception.
        """

        org = OrganizationFactory()
        user = UserFactory(orgs=[org])
        grant_role(user, org, RoleCode.AUTHOR)
        wf = WorkflowFactory(
            org=org,
            agent_access_enabled=False,
            is_public=False,
        )
        WorkflowAccessGrant.objects.create(
            workflow=wf,
            user=user,
            is_active=True,
        )

        visible = _visible_pks(user)
        assert wf.pk in visible


class TestMCPCatalogPublicBranches:
    """Public workflows are visible regardless of for_user access.

    Two flags qualify: ``is_public`` (platform-wide, also used by the
    web UI) and ``agent_public_discovery`` (cross-org agent catalog).
    The catalog must include both.
    """

    def test_is_public_workflow_visible_to_unrelated_user(self):
        author = UserFactory()
        public_wf = WorkflowFactory(
            user=author,
            is_public=True,
            agent_access_enabled=False,
        )

        unrelated = UserFactory(orgs=[])
        Membership.objects.filter(user=unrelated).delete()

        visible = _visible_pks(unrelated)
        assert public_wf.pk in visible

    def test_agent_public_discovery_visible_to_unrelated_user(self):
        """agent_public_discovery exposes a workflow to every MCP user.

        Unlike ``is_public``, this flag is MCP-specific — the author
        deliberately chose to make this row discoverable by external
        agents.

        The model's ``clean()`` cascades several fields when
        ``agent_public_discovery=True`` (forces x402 billing, requires
        a price, requires DO_NOT_STORE retention). We supply all of
        them so the row passes validation; the test focuses on the
        catalog visibility, not the publishing rules.
        """

        from validibot.submissions.constants import SubmissionRetention
        from validibot.workflows.constants import AgentBillingMode

        author = UserFactory()
        public_agent_wf = WorkflowFactory(
            user=author,
            agent_public_discovery=True,
            agent_access_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=100,
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )

        unrelated = UserFactory(orgs=[])
        Membership.objects.filter(user=unrelated).delete()

        visible = _visible_pks(unrelated)
        assert public_agent_wf.pk in visible
