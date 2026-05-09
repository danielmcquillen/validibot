"""Tests for ``OrgMembershipPermission``.

The permission class gates org-scoped REST endpoints. It must accept
the three legitimate access paths to an org:

1. Active ``Membership`` in the org.
2. Active ``WorkflowAccessGrant`` on any workflow in the org
   (per-workflow cross-org sharing).
3. Active ``OrgGuestAccess`` for the org (org-wide guest invite
   acceptance — the ALL-scope path).

Pin all three so a regression that drops the OrgGuestAccess branch
locks ALL-scope guests out of REST endpoints.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from django.test import RequestFactory

from validibot.core.api.org_scoped import OrgMembershipPermission
from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.models import OrgGuestAccess
from validibot.workflows.models import WorkflowAccessGrant
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _make_request(user):
    """Build a Django HttpRequest with ``user`` attached.

    The permission class only consults ``request.user``, which resolves
    on the raw Django request without going through DRF's auth pipeline
    — that pipeline would reject our directly-attached user as unverified.
    """
    factory = RequestFactory()
    request = factory.get("/dummy/")
    request.user = user
    return request


def _stub_view(*, org):
    """Mock view object that satisfies the permission's interface.

    The permission delegates to ``view.get_membership()`` and
    ``view.get_org()`` — replicating those is enough.
    """
    view = MagicMock()
    view.get_org.return_value = org
    view.get_membership.return_value = None  # default: no membership
    return view


class TestOrgMembershipPermission:
    """Permission grants access via membership, grant, OR org-wide guest access."""

    def test_member_is_granted(self):
        """The membership branch — pre-existing behaviour, pinned for safety."""

        org = OrganizationFactory()
        user = UserFactory(orgs=[org])
        grant_role(user, org, RoleCode.AUTHOR)

        permission = OrgMembershipPermission()
        request = _make_request(user)
        view = _stub_view(org=org)
        view.get_membership.return_value = Membership.objects.get(
            user=user,
            org=org,
        )

        assert permission.has_permission(request, view) is True

    def test_per_workflow_grant_is_granted(self):
        """The per-workflow grant branch — also pre-existing.

        Pin so future refactors can't drop it accidentally.
        """

        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        WorkflowAccessGrant.objects.create(
            workflow=WorkflowFactory(org=org),
            user=guest,
            is_active=True,
        )

        permission = OrgMembershipPermission()
        request = _make_request(guest)
        view = _stub_view(org=org)

        assert permission.has_permission(request, view) is True

    def test_org_guest_access_is_granted(self):
        """The org-wide guest access branch — added by this fix.

        Without this branch, an ALL-scope guest is rejected at the
        permission layer before queryset narrowing runs, even though
        ``Workflow.objects.for_user`` would have shown them workflows.
        """

        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        OrgGuestAccess.objects.create(user=guest, org=org, is_active=True)

        permission = OrgMembershipPermission()
        request = _make_request(guest)
        view = _stub_view(org=org)

        assert permission.has_permission(request, view) is True

    def test_inactive_org_guest_access_is_not_granted(self):
        """Revoked guest access (is_active=False) must NOT grant permission.

        Pin: revocation works through the same flag-flip pattern as
        per-workflow grants, and the permission layer must respect it.
        """

        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        OrgGuestAccess.objects.create(user=guest, org=org, is_active=False)

        permission = OrgMembershipPermission()
        request = _make_request(guest)
        view = _stub_view(org=org)

        assert permission.has_permission(request, view) is False

    def test_unrelated_user_is_not_granted(self):
        """No membership, no grant, no org-guest-access → denied."""

        org = OrganizationFactory()
        unrelated = UserFactory(orgs=[])
        Membership.objects.filter(user=unrelated).delete()

        permission = OrgMembershipPermission()
        request = _make_request(unrelated)
        view = _stub_view(org=org)

        assert permission.has_permission(request, view) is False

    def test_org_guest_access_in_one_org_does_not_leak_to_another(self):
        """An OrgGuestAccess for Org A must not grant permission for Org B.

        Multi-tenant isolation pin — the permission's org filter uses
        the URL's org context, so a guest's row in another org doesn't
        accidentally escape across the tenant boundary.
        """

        org_a = OrganizationFactory()
        org_b = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        OrgGuestAccess.objects.create(user=guest, org=org_a, is_active=True)

        permission = OrgMembershipPermission()
        request = _make_request(guest)

        view_a = _stub_view(org=org_a)
        view_b = _stub_view(org=org_b)

        assert permission.has_permission(request, view_a) is True
        assert permission.has_permission(request, view_b) is False

    def test_superuser_is_always_granted(self):
        """Superuser bypass is honoured ahead of any other branch."""

        org = OrganizationFactory()
        superuser = UserFactory(orgs=[])
        superuser.is_superuser = True
        superuser.save()

        permission = OrgMembershipPermission()
        request = _make_request(superuser)
        view = _stub_view(org=org)

        assert permission.has_permission(request, view) is True
