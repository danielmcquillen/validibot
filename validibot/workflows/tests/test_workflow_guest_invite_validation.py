"""Email-validation tests for ``WorkflowGuestInviteView``.

``invitee_email`` is persisted on ``WorkflowInvite`` and later rendered in
notification templates, so an unvalidated value is a stored-XSS and
data-integrity risk. The view now rejects malformed addresses (Django's
``validate_email``) and caps length at RFC 5321's 254 characters *before*
creating an invite.

Regression for ADR 04-23 review-ep-#9, where the only check was non-empty —
so a payload like ``<script>...`` was accepted and stored.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.models import WorkflowInvite
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _login_admin(client, workflow):
    """Log in an ADMIN of the workflow's org (may manage sharing for it)."""
    admin = UserFactory()
    grant_role(admin, workflow.org, RoleCode.ADMIN)
    admin.set_current_org(workflow.org)
    client.force_login(admin)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()
    return admin


def _post_invite(client, workflow, email):
    return client.post(
        reverse("workflows:workflow_guest_invite", args=[workflow.pk]),
        data={"email": email},
    )


def test_xss_payload_email_creates_no_invite(client):
    """A script-payload "email" must be rejected, never stored.

    ``<script>alert(1)</script>`` is not a valid address; ``validate_email``
    rejects it, so no ``WorkflowInvite`` is created and the payload never
    reaches ``invitee_email`` (which would be a stored-XSS vector if a
    notification template rendered it unescaped).
    """
    workflow = WorkflowFactory(user=UserFactory())
    _login_admin(client, workflow)

    _post_invite(client, workflow, "<script>alert(1)</script>")

    assert not WorkflowInvite.objects.exists()


def test_malformed_email_creates_no_invite(client):
    """A plainly malformed address ("not-an-email") creates no invite."""
    workflow = WorkflowFactory(user=UserFactory())
    _login_admin(client, workflow)

    _post_invite(client, workflow, "not-an-email")

    assert not WorkflowInvite.objects.exists()


def test_overlong_email_creates_no_invite(client):
    """An address beyond the 254-char cap is rejected by the length guard.

    The length check runs before ``validate_email``, so a clearly over-cap
    value (300 chars) exercises that guard specifically.
    """
    workflow = WorkflowFactory(user=UserFactory())
    _login_admin(client, workflow)

    _post_invite(client, workflow, "a" * 300)

    assert not WorkflowInvite.objects.exists()
