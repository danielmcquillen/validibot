"""Tests for the workflow access + agent-workflow resolvers.

Phase 2 of ADR-2026-04-27 (trust-boundary): consolidate object-level
workflow access decisions and latest-version selection into two
focused service classes. These tests pin down the resolvers'
behavior in isolation. Path-level integration tests
(``test_workflow_api_permissions.py`` and the x402 suite) verify
the resolvers are correctly wired into each calling site.

Why isolate resolver tests from path tests
==========================================

The resolvers are pure data-access logic — they take a user (or none,
for the agent variant) and return a queryset / single workflow. The
calling paths add HTTP serialization, URL routing, exception
mapping, and authentication. Testing the resolver in isolation lets
us verify decisions like "guest-with-grant sees their workflow but
nothing else" without needing to construct full HTTP requests.

Adding a new access rule (e.g. team-based access in a future ADR)
lands as a new test class here, plus path-level tests that the new
rule shows up in each path's filtered list. The resolver tests
verify the rule's logic; the path tests verify the rule's reach.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.constants import AgentBillingMode
from validibot.workflows.services.access import WorkflowAccessResolver
from validibot.workflows.services.agent_workflows import AgentWorkflowResolver
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

User = get_user_model()
pytestmark = pytest.mark.django_db


# ──────────────────────────────────────────────────────────────────────────
# WorkflowAccessResolver
# ──────────────────────────────────────────────────────────────────────────


class WorkflowAccessResolverListForUserTests(TestCase):
    """``list_for_user`` returns only workflows the user can access.

    This covers the canonical ADR-2026-04-27 ``[trust-#1]`` scenario:
    a guest with one grant should not see other workflows in the
    same org. The resolver is now the single decision point for
    that scoping.
    """

    def test_unauthenticated_user_sees_nothing(self):
        """``WorkflowQuerySet.for_user`` returns ``.none()`` for anonymous.

        The resolver inherits this behavior; verifying it here keeps
        the contract obvious to future readers (and catches a
        regression where someone might "fix" the resolver to fall
        back to all workflows if user is anonymous, which would be
        a serious access-control bug).
        """

        class _AnonymousUserStub:
            is_authenticated = False

        result = WorkflowAccessResolver.list_for_user(_AnonymousUserStub())
        assert result.count() == 0

    def test_member_sees_org_workflows_only(self):
        """An org member sees their org's workflows but not other orgs'."""
        org_a = OrganizationFactory()
        org_b = OrganizationFactory()
        wf_a = WorkflowFactory(org=org_a, is_active=True)
        WorkflowFactory(org=org_b, is_active=True)  # not visible

        member = UserFactory()
        grant_role(member, org_a, RoleCode.EXECUTOR)

        result = WorkflowAccessResolver.list_for_user(member, org_id=org_a.id)
        assert list(result) == [wf_a]

    def test_default_filters_exclude_inactive_archived_tombstoned(self):
        """Default filters apply all three lifecycle exclusions.

        Active+visible workflow appears; inactive, archived, and
        tombstoned ones don't.
        """
        org = OrganizationFactory()
        member = UserFactory()
        grant_role(member, org, RoleCode.EXECUTOR)

        visible = WorkflowFactory(org=org, is_active=True, is_archived=False)
        WorkflowFactory(org=org, is_active=False)  # inactive
        WorkflowFactory(org=org, is_archived=True)  # archived
        WorkflowFactory(org=org, is_tombstoned=True)  # tombstoned

        result = list(WorkflowAccessResolver.list_for_user(member, org_id=org.id))
        assert visible in result
        assert len(result) == 1


class WorkflowAccessResolverGetForUserTests(TestCase):
    """``get_for_user`` returns a single workflow or None."""

    def test_returns_workflow_when_user_can_access(self):
        org = OrganizationFactory()
        member = UserFactory()
        grant_role(member, org, RoleCode.EXECUTOR)
        workflow = WorkflowFactory(org=org, is_active=True)

        result = WorkflowAccessResolver.get_for_user(
            member,
            slug=workflow.slug,
            org_id=org.id,
        )
        assert result == workflow

    def test_returns_none_when_user_cannot_access(self):
        """Different-org workflow returns None, not a 403.

        The resolver doesn't distinguish "doesn't exist" from
        "exists but not visible to you" because that distinction
        leaks information (an attacker could enumerate slugs by
        observing 403 vs 404). Path callers translate None to 404.
        """
        org_a = OrganizationFactory()
        org_b = OrganizationFactory()
        WorkflowFactory(org=org_a, is_active=True, slug="other-org-flow")

        member = UserFactory()
        grant_role(member, org_b, RoleCode.EXECUTOR)

        result = WorkflowAccessResolver.get_for_user(
            member,
            slug="other-org-flow",
            org_id=org_a.id,
        )
        assert result is None

    def test_requires_exactly_one_of_slug_or_pk(self):
        """Defensive: pass exactly one identifier."""
        member = UserFactory()
        with pytest.raises(ValueError, match="exactly one"):
            WorkflowAccessResolver.get_for_user(member)
        with pytest.raises(ValueError, match="exactly one"):
            WorkflowAccessResolver.get_for_user(member, slug="x", pk=1)

    def test_slug_lookup_returns_latest_version(self):
        """When a slug has multiple versions, get_for_user returns latest."""
        org = OrganizationFactory()
        member = UserFactory()
        grant_role(member, org, RoleCode.EXECUTOR)
        v1 = WorkflowFactory(org=org, slug="versioned", version="1", is_active=True)
        v2 = WorkflowFactory(
            org=org,
            slug="versioned",
            version="2",
            is_active=True,
        )

        result = WorkflowAccessResolver.get_for_user(
            member,
            slug="versioned",
            org_id=org.id,
        )
        # Both versions are valid; expect the latest.
        assert result in (v1, v2)
        # Specifically, the version_utils helper picks v2 (higher
        # parsed version). If that ever changes, this test catches
        # it.
        assert result == v2


# ──────────────────────────────────────────────────────────────────────────
# AgentWorkflowResolver
# ──────────────────────────────────────────────────────────────────────────


class AgentWorkflowResolverListPublishedTests(TestCase):
    """``list_published`` returns only public-discovery workflows."""

    def test_excludes_private_workflows(self):
        """Workflows not published for agent discovery don't appear."""
        org = OrganizationFactory()
        WorkflowFactory(
            org=org,
            agent_public_discovery=True,
            agent_access_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            is_active=True,
        )
        WorkflowFactory(
            org=org,
            agent_public_discovery=False,  # private
            is_active=True,
        )

        result = AgentWorkflowResolver.list_published()
        public_count = sum(1 for w in result if w.org_id == org.id)
        # Only the public one.
        assert public_count == 1

    def test_excludes_inactive_and_tombstoned(self):
        """Inactive or tombstoned workflows don't appear in the public list."""
        org = OrganizationFactory()
        active = WorkflowFactory(
            org=org,
            agent_public_discovery=True,
            agent_access_enabled=True,
            agent_price_cents=10,
            is_active=True,
        )
        WorkflowFactory(
            org=org,
            agent_public_discovery=True,
            agent_access_enabled=True,
            agent_price_cents=10,
            is_active=False,
        )  # inactive
        WorkflowFactory(
            org=org,
            agent_public_discovery=True,
            agent_access_enabled=True,
            agent_price_cents=10,
            is_tombstoned=True,
        )  # tombstoned

        result = AgentWorkflowResolver.list_published()
        for wf in result:
            if wf.org_id == org.id:
                assert wf.id == active.id

    def test_returns_only_latest_version_per_slug(self):
        """Versioned workflow families appear once (latest version)."""
        org = OrganizationFactory()
        v1 = WorkflowFactory(
            org=org,
            slug="versioned-public",
            version="1",
            agent_public_discovery=True,
            agent_access_enabled=True,
            agent_price_cents=10,
            is_active=True,
        )
        v2 = WorkflowFactory(
            org=org,
            slug="versioned-public",
            version="2",
            agent_public_discovery=True,
            agent_access_enabled=True,
            agent_price_cents=10,
            is_active=True,
        )

        result = AgentWorkflowResolver.list_published()
        matches = [w for w in result if w.slug == "versioned-public"]
        # Exactly one entry — the latest.
        assert len(matches) == 1
        assert matches[0] == v2
        assert v1 not in matches


class AgentWorkflowResolverGetBySlugTests(TestCase):
    """``get_by_slug`` returns the latest published version."""

    def test_returns_latest_version(self):
        """Versioned slug resolves to latest active published version."""
        org = OrganizationFactory()
        v1 = WorkflowFactory(
            org=org,
            slug="versioned-public",
            version="1",
            agent_public_discovery=True,
            agent_access_enabled=True,
            agent_price_cents=10,
            is_active=True,
        )
        v2 = WorkflowFactory(
            org=org,
            slug="versioned-public",
            version="2",
            agent_public_discovery=True,
            agent_access_enabled=True,
            agent_price_cents=10,
            is_active=True,
        )

        result = AgentWorkflowResolver.get_by_slug(
            org_slug=org.slug,
            workflow_slug="versioned-public",
        )
        assert result == v2
        assert result != v1

    def test_returns_none_for_private_workflow(self):
        """Workflows not published for agent discovery return None.

        This is the primary discovery filter — only published
        workflows are visible to anonymous agents through this
        resolver.
        """
        org = OrganizationFactory()
        WorkflowFactory(
            org=org,
            slug="private-flow",
            agent_public_discovery=False,
            is_active=True,
        )

        result = AgentWorkflowResolver.get_by_slug(
            org_slug=org.slug,
            workflow_slug="private-flow",
        )
        assert result is None

    def test_returns_none_for_unknown_slug(self):
        """Unknown slug returns None (translated to 404 by the caller)."""
        org = OrganizationFactory()
        result = AgentWorkflowResolver.get_by_slug(
            org_slug=org.slug,
            workflow_slug="does-not-exist",
        )
        assert result is None


class AgentWorkflowResolverGetBySlugForX402Tests(TestCase):
    """``get_by_slug_for_x402`` is the relaxed variant for x402 payment paths."""

    def test_returns_workflow_even_when_not_published_for_discovery(self):
        """x402 path needs to distinguish "not found" from "not published".

        Without the relaxed variant, x402 would return 404 for both
        cases — which would force agents to guess whether the slug
        is wrong or the workflow isn't payable. The relaxed variant
        returns the workflow so x402 can do its own publishing
        check (``_ensure_public_x402_workflow``) and return a more
        specific error (FORBIDDEN with "not published for x402"
        instead of NOT_FOUND).
        """
        org = OrganizationFactory()
        workflow = WorkflowFactory(
            org=org,
            slug="x402-flow",
            agent_public_discovery=False,
            agent_access_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=10,
            is_active=True,
        )
        WorkflowStepFactory(workflow=workflow)

        result = AgentWorkflowResolver.get_by_slug_for_x402(
            org_slug=org.slug,
            workflow_slug="x402-flow",
        )
        assert result == workflow

    def test_returns_none_for_unknown_slug(self):
        """Unknown slug still returns None (no workflow at all)."""
        org = OrganizationFactory()
        result = AgentWorkflowResolver.get_by_slug_for_x402(
            org_slug=org.slug,
            workflow_slug="does-not-exist",
        )
        assert result is None

    def test_excludes_inactive(self):
        """Inactive workflows return None even on the relaxed variant."""
        org = OrganizationFactory()
        WorkflowFactory(
            org=org,
            slug="inactive-x402",
            is_active=False,
        )
        result = AgentWorkflowResolver.get_by_slug_for_x402(
            org_slug=org.slug,
            workflow_slug="inactive-x402",
        )
        assert result is None
