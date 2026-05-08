"""End-to-end capability matrix for GUEST-classified accounts.

This module is the executable counterpart to the documented Guest
Capability Matrix. Every row maps to one or more tests that pin what
GUEST users can and cannot do across the platform. A regression in
any single row means a guest gained or lost a capability they
shouldn't have, and the failure here surfaces it.

Tests are organised into two top-level classes:

* :class:`TestGuestCannotDo` — actions that must return 403 / 404 /
  ValidationError for a GUEST user. The enforcement mechanism is
  noted in each test docstring so a failure points reviewers at the
  right guard (``Membership.clean``, ``OrgPermissionBackend``,
  ``for_user()`` narrowing, ``is_superuser``, or a SiteSettings flag).

* :class:`TestGuestCanDo` — actions that must succeed for a GUEST user.
  These confirm the gates aren't *over*-restrictive — a guest who
  can't do anything is just as broken as one who can do too much.

All tests pin a Pro license that activates ``guest_management`` so the
sticky-guest layer is in play. Without Pro, GUEST classification
doesn't exist at all and the matrix is moot.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import set_license
from validibot.users.constants import RoleCode
from validibot.users.constants import UserKindGroup
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.users.user_kind import classify_as_basic
from validibot.users.user_kind import classify_as_guest

pytestmark = pytest.mark.django_db


def _pro_license_with_guest_management() -> License:
    """Pro license that activates ``guest_management`` + supporting features.

    The capability matrix exercises paths that touch audit logging
    (group changes), so AUDIT_LOG is also activated.
    """
    return License(
        edition=Edition.PRO,
        features=frozenset(
            {
                CommercialFeature.GUEST_MANAGEMENT.value,
                CommercialFeature.AUDIT_LOG.value,
            },
        ),
    )


def _setup_guest_user():
    """Create a GUEST-classified user with no memberships.

    Mirrors the canonical sticky-guest state: in the Guests group,
    holding zero org memberships. The fixture-style helper keeps
    individual tests focused on the one capability they pin.
    """
    user = UserFactory(orgs=[])
    Membership.objects.filter(user=user).delete()
    classify_as_guest(user)
    user.set_password("correct-horse-battery-staple")
    user.save()
    assert user.user_kind == UserKindGroup.GUEST
    return user


def _login_as(client, user):
    """Force-login + set up the session like a real authenticated request."""
    client.force_login(user)
    return client


# =============================================================================
# What guests CANNOT do
# =============================================================================


class TestGuestCannotDo:
    """Capabilities that must be blocked for GUEST-classified accounts.

    Every test names the enforcement layer that rejects the action so
    a regression points at the failing guard. The matrix splits into
    sub-classes per category so test failures surface in groups and
    a single-category bug doesn't drown out everything.
    """

    # ------------------------------------------------------------------
    # System & account
    # ------------------------------------------------------------------

    def test_cannot_be_added_as_membership(self):
        """Guard: ``Membership.clean()`` raises ValidationError.

        Sticky semantics: a GUEST user cannot become an org member by
        any data-layer path, including direct ORM creates, fixtures,
        and admin shortcuts. The guard runs in ``full_clean`` which
        ``Membership.save`` invokes on every write.
        """

        set_license(_pro_license_with_guest_management())
        guest = _setup_guest_user()
        org = OrganizationFactory()

        with pytest.raises(ValidationError):
            Membership.objects.create(user=guest, org=org, is_active=True)

        assert not Membership.objects.filter(user=guest, org=org).exists()

    def test_cannot_log_in_when_kill_switch_enabled(self, client):
        """Guard: ``allow_guest_access`` site flag (adapter pre_login).

        The operator's kill switch is the SiteSettings flag; flipping
        it OFF must immediately deny GUEST login regardless of
        otherwise-valid credentials.
        """

        set_license(_pro_license_with_guest_management())
        from validibot.core.site_settings import get_site_settings

        settings = get_site_settings()
        settings.allow_guest_access = False
        settings.save()

        guest = _setup_guest_user()
        response = client.post(
            reverse("account_login"),
            {"login": guest.email, "password": "correct-horse-battery-staple"},
            follow=True,
        )

        # Adapter redirects back to login with a flash message.
        assert response.redirect_chain
        final_url = response.redirect_chain[-1][0]
        assert reverse("account_login") in final_url

    # ------------------------------------------------------------------
    # Workflow management
    # ------------------------------------------------------------------

    def test_cannot_create_workflow(self, client):
        """Guard: ``form_valid`` rejects when ``get_current_org()`` is None.

        A guest's ``get_current_org`` returns None (the
        ``ensure_personal_workspace`` helper short-circuits for GUEST
        kinds), so the form_valid path adds a non-field error and
        returns ``form_invalid``. No ``Workflow`` row is persisted.

        Note: the GET path currently returns 200 with the form
        rendered, which is harmless (no row is ever created) but a
        small UX leak — the create form should arguably 403 for
        guests rather than render. Tracking that as a follow-up; the
        critical security boundary (no Workflow can be persisted by a
        guest) is what this test pins.
        """

        set_license(_pro_license_with_guest_management())
        from validibot.workflows.models import Workflow

        guest = _setup_guest_user()
        org = OrganizationFactory()
        _login_as(client, guest)

        session = client.session
        session["active_org_id"] = org.pk
        session.save()

        before_count = Workflow.objects.count()
        response = client.post(
            reverse("workflows:workflow_create"),
            {
                "name": "Sneaky Workflow",
                "description": "Should not be created",
            },
        )

        # Form re-renders with errors (200) or denial (403/404). We
        # only care that no row was persisted.
        assert Workflow.objects.count() == before_count
        # And ANY of these responses is acceptable — what matters is
        # the persistence guard.
        assert response.status_code in (
            HTTPStatus.OK,  # form re-rendered with errors
            HTTPStatus.FORBIDDEN,
            HTTPStatus.NOT_FOUND,
        )

    def test_cannot_invite_other_guests(self, client):
        """Guard: per-org RBAC (``GUEST_INVITE`` requires ADMIN/AUTHOR/OWNER).

        Guests have no role anywhere; the GUEST_INVITE permission
        check via ``OrgPermissionBackend`` rejects them.
        """

        set_license(_pro_license_with_guest_management())
        guest = _setup_guest_user()
        org = OrganizationFactory()
        _login_as(client, guest)

        session = client.session
        session["active_org_id"] = org.pk
        session.save()

        response = client.post(
            reverse("members:guest_invite_create"),
            {"email": "another@example.com", "scope": "SELECTED"},
        )
        # Permission denied OR not-found from missing org context.
        assert response.status_code in (HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND)

    # ------------------------------------------------------------------
    # Workflow visibility (read-side narrowing)
    # ------------------------------------------------------------------

    def test_cannot_see_workflows_outside_their_grants(self):
        """Guard: ``WorkflowQuerySet.for_user`` narrows visibility.

        A guest with no grants and no public-flag workflows in scope
        must see an EMPTY queryset. The for_user filter is the
        single source of truth for read-side narrowing.
        """

        set_license(_pro_license_with_guest_management())
        from validibot.workflows.models import Workflow
        from validibot.workflows.tests.factories import WorkflowFactory

        guest = _setup_guest_user()
        # Create workflows in an org the guest has no relationship to.
        org = OrganizationFactory()
        WorkflowFactory(org=org, is_public=False)
        WorkflowFactory(org=org, is_public=False)

        visible = Workflow.objects.for_user(guest)
        assert visible.count() == 0

    def test_can_only_see_workflows_via_explicit_grants(self):
        """Guard: ``WorkflowAccessGrant`` lookup in for_user.

        Granting access to one workflow in an org must NOT leak access
        to other workflows in that same org. Per-workflow grants are
        per-workflow.
        """

        set_license(_pro_license_with_guest_management())
        from validibot.workflows.models import Workflow
        from validibot.workflows.models import WorkflowAccessGrant
        from validibot.workflows.tests.factories import WorkflowFactory

        guest = _setup_guest_user()
        org = OrganizationFactory()
        granted_wf = WorkflowFactory(org=org, is_public=False)
        ungranted_wf = WorkflowFactory(org=org, is_public=False)
        WorkflowAccessGrant.objects.create(
            workflow=granted_wf,
            user=guest,
            is_active=True,
        )

        visible_pks = set(
            Workflow.objects.for_user(guest).values_list("pk", flat=True),
        )
        assert granted_wf.pk in visible_pks
        assert ungranted_wf.pk not in visible_pks


# =============================================================================
# What guests CAN do
# =============================================================================


class TestGuestCanDo:
    """Capabilities that must succeed for GUEST-classified accounts.

    Pinning the *positive* side prevents over-restriction: a future
    refactor that accidentally tightens an inactive permission check
    would silently strip guests of legitimate capabilities (running
    workflows they were granted, viewing public workflows, accessing
    their own personal data). These tests fail in that case.
    """

    def test_can_log_in_when_flag_enabled(self, client):
        """Default state: ``allow_guest_access=True`` → guests log in."""

        set_license(_pro_license_with_guest_management())
        # Default allow_guest_access is True — no need to flip.
        guest = _setup_guest_user()

        response = client.post(
            reverse("account_login"),
            {"login": guest.email, "password": "correct-horse-battery-staple"},
            follow=True,
        )

        # Login should not bounce back to /accounts/login/.
        if response.redirect_chain:
            final_url = response.redirect_chain[-1][0]
            assert reverse("account_login") not in final_url

    def test_can_view_workflows_with_active_grant(self):
        """Guard: per-workflow grant resolved by for_user.

        The whole point of the cross-org-share path: a guest with a
        ``WorkflowAccessGrant`` for a workflow can see that workflow
        in their queryset.
        """

        set_license(_pro_license_with_guest_management())
        from validibot.workflows.models import Workflow
        from validibot.workflows.models import WorkflowAccessGrant
        from validibot.workflows.tests.factories import WorkflowFactory

        guest = _setup_guest_user()
        wf = WorkflowFactory(is_public=False)
        WorkflowAccessGrant.objects.create(workflow=wf, user=guest, is_active=True)

        visible = Workflow.objects.for_user(guest)
        assert wf.pk in set(visible.values_list("pk", flat=True))

    def test_can_view_workflows_via_org_guest_access(self):
        """Guard: ``OrgGuestAccess`` resolved by for_user.

        The "100 workflows, 10 new a month" simplification: one
        OrgGuestAccess row authorises every workflow in the org.
        """

        set_license(_pro_license_with_guest_management())
        from validibot.workflows.models import OrgGuestAccess
        from validibot.workflows.models import Workflow
        from validibot.workflows.tests.factories import WorkflowFactory

        guest = _setup_guest_user()
        org = OrganizationFactory()
        OrgGuestAccess.objects.create(user=guest, org=org, is_active=True)
        wf1 = WorkflowFactory(org=org)
        wf2 = WorkflowFactory(org=org)

        visible_pks = set(
            Workflow.objects.for_user(guest).values_list("pk", flat=True),
        )
        assert wf1.pk in visible_pks
        assert wf2.pk in visible_pks

    def test_can_view_public_workflows(self):
        """Guard: ``is_public=True`` resolved by for_user.

        Platform-wide public workflows are visible to every
        authenticated user, including guests.
        """

        set_license(_pro_license_with_guest_management())
        from validibot.workflows.models import Workflow
        from validibot.workflows.tests.factories import WorkflowFactory

        guest = _setup_guest_user()
        public_wf = WorkflowFactory(is_public=True)

        visible_pks = set(
            Workflow.objects.for_user(guest).values_list("pk", flat=True),
        )
        assert public_wf.pk in visible_pks

    def test_basic_user_remains_basic_after_grant(self):
        """Sticky cross-org sharing: a BASIC user keeps their kind.

        A user with a Membership in Org A who is granted a workflow
        in Org B does NOT become GUEST. The classification is sticky
        and orthogonal to per-workflow access.
        """

        set_license(_pro_license_with_guest_management())
        from validibot.workflows.models import WorkflowAccessGrant
        from validibot.workflows.tests.factories import WorkflowFactory

        org_a = OrganizationFactory()
        org_b = OrganizationFactory()
        user = UserFactory(orgs=[org_a])
        grant_role(user, org_a, RoleCode.AUTHOR)
        classify_as_basic(user)
        wf_in_b = WorkflowFactory(org=org_b)
        WorkflowAccessGrant.objects.create(
            workflow=wf_in_b,
            user=user,
            is_active=True,
        )

        # Even with the cross-org grant, the user remains BASIC —
        # they retain their membership in Org A so the system kind
        # tracks the membership, not the grant.
        assert user.user_kind == UserKindGroup.BASIC


# =============================================================================
# Boundary conditions: kind transitions
# =============================================================================


class TestGuestCapabilityBoundaries:
    """Behaviour at the edges of the GUEST/BASIC boundary.

    Most matrix tests check a single row in isolation. These tests
    verify what happens when a user *transitions* — promoted from
    GUEST to BASIC, or demoted from BASIC to GUEST. The transition
    is the load-bearing operation in incident response (an operator
    cuts off a compromised guest by demoting + revoking access in
    one motion); regressions here can break recovery.
    """

    def test_promoted_user_can_be_added_as_member(self):
        """After promote_user, the Membership.clean guard no longer fires.

        Pin: the guard reads ``user.user_kind`` so flipping the
        classifier in the same transaction immediately unblocks
        membership creation. No cache, no stale state, no eventual
        consistency.
        """

        set_license(_pro_license_with_guest_management())
        guest = _setup_guest_user()

        from validibot.users.management.commands.promote_user import (
            promote_user_to_basic,
        )

        promote_user_to_basic(target=guest, actor=None)
        guest.refresh_from_db()
        assert guest.user_kind == UserKindGroup.BASIC

        # Now membership creation must succeed.
        new_org = OrganizationFactory()
        membership = Membership.objects.create(
            user=guest,
            org=new_org,
            is_active=True,
        )
        assert membership.pk is not None

    def test_demoted_user_loses_membership_creation_capability(self):
        """After demote, future Membership.create raises ValidationError.

        Existing memberships are NOT removed (operator must clean
        those up separately) but the guard immediately blocks
        adding new memberships. This is the safer half of demotion:
        a typo can be reverted by promoting back without losing data.
        """

        set_license(_pro_license_with_guest_management())
        user = UserFactory(orgs=[])
        classify_as_basic(user)

        from validibot.users.management.commands.promote_user import (
            demote_user_to_guest,
        )

        demote_user_to_guest(target=user, actor=None)
        user.refresh_from_db()
        assert user.user_kind == UserKindGroup.GUEST

        # New membership creation must now fail.
        new_org = OrganizationFactory()
        with pytest.raises(ValidationError):
            Membership.objects.create(user=user, org=new_org, is_active=True)
