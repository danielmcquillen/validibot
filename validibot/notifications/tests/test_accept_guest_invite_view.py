"""Regression tests for ``AcceptGuestInviteView``.

The notification accept path consumes the return value of
``GuestInvite.accept()``. Since ``accept()`` now returns either a
``list[WorkflowAccessGrant]`` (SELECTED scope) or a single
``OrgGuestAccess`` (ALL scope), the view must handle both shapes —
in particular, it must NOT call ``len()`` on the union directly.

These tests pin both branches end-to-end via the Django test client
so a regression that reverts to the old "always treat as list"
behaviour fails loudly with a TypeError + assertion.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.notifications.models import Notification
from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.models import GuestInvite
from validibot.workflows.models import OrgGuestAccess
from validibot.workflows.models import WorkflowAccessGrant
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _create_invite_and_notification(
    *,
    org,
    inviter,
    invitee,
    scope,
    workflows=None,
):
    """Build a GuestInvite + linked Notification for the invitee.

    Mirrors what ``GuestInviteCreateView`` does in production:
    creates the invite then a notification pointing at it.
    """

    invite = GuestInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_email=invitee.email,
        invitee_user=invitee,
        scope=scope,
        workflows=list(workflows or []),
        send_email=False,
    )
    notification = Notification.objects.create(
        user=invitee,
        org=org,
        type=Notification.Type.GUEST_INVITE,
        guest_invite=invite,
        payload={"message": "you've been invited"},
    )
    return invite, notification


class TestAcceptGuestInviteView:
    """The view dispatches both SELECTED and ALL acceptance shapes safely."""

    def test_selected_scope_succeeds_and_creates_per_workflow_grants(
        self,
        client,
    ):
        """Legacy shape — the per-workflow grant list must work as before.

        Pin the SELECTED branch so that fixing the ALL crash doesn't
        accidentally regress the SELECTED happy path.
        """

        org = OrganizationFactory()
        inviter = UserFactory(orgs=[org])
        grant_role(inviter, org, RoleCode.AUTHOR)
        invitee = UserFactory(orgs=[])
        Membership.objects.filter(user=invitee).delete()

        wf1 = WorkflowFactory(org=org)
        wf2 = WorkflowFactory(org=org)

        _, notification = _create_invite_and_notification(
            org=org,
            inviter=inviter,
            invitee=invitee,
            scope=GuestInvite.Scope.SELECTED,
            workflows=[wf1, wf2],
        )

        client.force_login(invitee)
        response = client.post(
            reverse(
                "notifications:notification-guest-invite-accept",
                kwargs={"pk": notification.pk},
            ),
        )

        # Redirect (302) on the non-HTMX path — view sends the user
        # back to the notification list.
        assert response.status_code == HTTPStatus.FOUND
        # Two WorkflowAccessGrants exist now.
        assert (
            WorkflowAccessGrant.objects.filter(
                user=invitee,
                workflow__org=org,
                is_active=True,
            ).count()
            == 2  # noqa: PLR2004
        )
        # No OrgGuestAccess for SELECTED scope.
        assert not OrgGuestAccess.objects.filter(user=invitee, org=org).exists()

    def test_all_scope_succeeds_and_creates_org_guest_access(self, client):
        """Regression: ALL scope must NOT crash on len(OrgGuestAccess).

        The pre-fix code did ``len(grants)`` on the return value
        unconditionally; for ALL scope that's an OrgGuestAccess
        instance (no ``__len__``), so the view raised TypeError.
        After the fix, the view branches on the result type.
        """

        org = OrganizationFactory()
        inviter = UserFactory(orgs=[org])
        grant_role(inviter, org, RoleCode.AUTHOR)
        invitee = UserFactory(orgs=[])
        Membership.objects.filter(user=invitee).delete()

        WorkflowFactory(org=org)
        WorkflowFactory(org=org)

        _, notification = _create_invite_and_notification(
            org=org,
            inviter=inviter,
            invitee=invitee,
            scope=GuestInvite.Scope.ALL,
        )

        client.force_login(invitee)
        response = client.post(
            reverse(
                "notifications:notification-guest-invite-accept",
                kwargs={"pk": notification.pk},
            ),
        )

        # Should redirect, not 500.
        assert response.status_code == HTTPStatus.FOUND
        # Exactly one OrgGuestAccess was created (not N grants).
        assert (
            OrgGuestAccess.objects.filter(
                user=invitee,
                org=org,
                is_active=True,
            ).count()
            == 1
        )
        assert not WorkflowAccessGrant.objects.filter(
            user=invitee,
            workflow__org=org,
        ).exists()

    def test_all_scope_htmx_path_also_works(self, client):
        """HTMX-flavoured response must also handle the OrgGuestAccess shape.

        Same crash surface, slightly different return code path. The
        HTMX branch renders a partial (200) instead of redirecting; pin
        both so neither regresses.
        """

        org = OrganizationFactory()
        inviter = UserFactory(orgs=[org])
        grant_role(inviter, org, RoleCode.AUTHOR)
        invitee = UserFactory(orgs=[])
        Membership.objects.filter(user=invitee).delete()
        WorkflowFactory(org=org)

        _, notification = _create_invite_and_notification(
            org=org,
            inviter=inviter,
            invitee=invitee,
            scope=GuestInvite.Scope.ALL,
        )

        client.force_login(invitee)
        response = client.post(
            reverse(
                "notifications:notification-guest-invite-accept",
                kwargs={"pk": notification.pk},
            ),
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == HTTPStatus.OK
