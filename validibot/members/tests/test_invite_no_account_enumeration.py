"""Regression tests: the member-invite form is not an account-enumeration oracle.

WHY THIS SUITE EXISTS
---------------------
``InviteUserForm`` lets an org admin invite someone by typing a raw email or
picking a type-ahead suggestion. The type-ahead search (``InviteSearchView``)
was deliberately hardened to surface only users the org already has a
relationship with, so an admin cannot enumerate accounts in other tenants. But
the *form* still (a) resolved any raw-typed email to an existing account and
(b) resolved any submitted ``invitee_user`` PK, after which the confirmation
dialog surfaced that account's identity (name, username, email, avatar).
Together those re-opened the very enumeration oracle the search view closed: an
admin could probe an arbitrary email or PK and learn whether it belonged to a
Validibot account, and whose it was.

The fix keeps a raw-typed address opaque in ``clean`` (no global lookup; the
existing-account binding moves to ``save``, where its result is never surfaced)
and scopes the ``invitee_user`` PK resolution to the same related-user set the
search view uses. These tests pin both halves, plus the property that the
useful behaviour — binding a registered invitee so they get an in-app
notification — still happens in ``save``.
"""

from __future__ import annotations

import pytest

from validibot.users.forms import InviteUserForm
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def org_and_admin():
    """An organization with an active admin who can issue invites."""
    org = OrganizationFactory()
    admin = UserFactory(orgs=[])
    admin.memberships.create(org=org, is_active=True)
    return org, admin


def test_raw_typed_email_of_existing_account_is_not_resolved_in_clean(org_and_admin):
    """A raw-typed email must NOT be resolved to an account during validation.

    This is the core anti-enumeration guard. If ``clean`` looked the address up
    and the confirmation dialog then showed the matched account, an admin could
    probe any email to learn whether it has a Validibot account (and whose it
    is). We type the email of an existing user and assert the cleaned
    ``invitee_user`` stays ``None`` — the dialog has nothing to reveal.
    """
    org, admin = org_and_admin
    existing = UserFactory(orgs=[])

    form = InviteUserForm(
        data={"search": existing.email, "roles": []},
        organization=org,
        inviter=admin,
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["invitee_user"] is None
    assert form.cleaned_data["invitee_email"] == existing.email


def test_arbitrary_user_id_outside_related_set_is_rejected(org_and_admin):
    """A submitted PK for a stranger must be refused, not resolved to identity.

    The type-ahead is scoped in the view, but a hand-crafted POST could carry
    any ``invitee_user`` PK. If the form resolved it, the confirmation dialog
    would expose that stranger's identity — a per-PK enumeration oracle. We
    submit the PK of a user the org has no relationship with and assert the form
    is invalid with the same generic message a missing user gets, leaking no
    existence signal.
    """
    org, admin = org_and_admin
    stranger = UserFactory(orgs=[])

    form = InviteUserForm(
        data={"search": "lookup", "invitee_user": stranger.pk, "roles": []},
        organization=org,
        inviter=admin,
    )

    assert not form.is_valid()
    assert "does not exist" in " ".join(form.errors.get("__all__", []))


def test_save_binds_existing_account_server_side(org_and_admin):
    """``save`` still binds a raw-typed email to its existing account.

    The privacy fix must not lose the useful behaviour: a registered invitee
    should still receive an in-app notification rather than an email. That
    binding moves from ``clean`` (where it leaked) to ``save`` (where the result
    is never surfaced to the admin). We assert the created invite is bound to
    the existing user even though ``clean`` left it unresolved.
    """
    org, admin = org_and_admin
    existing = UserFactory(orgs=[])

    form = InviteUserForm(
        data={"search": existing.email, "roles": []},
        organization=org,
        inviter=admin,
    )
    assert form.is_valid(), form.errors

    invite = form.save()

    assert invite.invitee_user == existing
    assert invite.invitee_email == existing.email
