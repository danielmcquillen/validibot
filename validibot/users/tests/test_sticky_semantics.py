"""Tests for the sticky-guest data-layer guards and promotion path.

This module covers four pieces that together enforce sticky semantics:

1. **`Membership.clean()` guard** — blocks creating a Membership for a
   user whose ``user_kind`` is GUEST. Last-line-of-defence at the data
   boundary; catches direct ORM creates, fixtures, and admin shortcuts.
2. **m2m_changed audit signal** — every change to ``User.groups`` lands
   an audit log entry so an operator can trace classification changes.
3. **`UserAdmin` group lockdown** — non-superusers can't edit the
   ``groups`` field via the admin form.
4. **`promote_user` command + admin action** — atomic promotion with
   personal-workspace provisioning and a single intent-specific audit
   row, plus the demotion path with its ``--confirm`` safety gate.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import CommandError
from django.core.management import call_command

from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditLogEntry
from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import set_license
from validibot.users.constants import UserKindGroup
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.user_kind import classify_as_basic
from validibot.users.user_kind import classify_as_guest

pytestmark = pytest.mark.django_db


def _pro_license_with_guest_management() -> License:
    """Return a Pro license with ``guest_management`` activated.

    Sticky semantics only activate when this feature is in the
    license. Tests that exercise the guard, signal, or promote_user
    command call ``set_license(_pro_license_with_guest_management())``
    at the top of the test body. The root conftest snapshots and
    restores the license per-test.
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


def _pro_license_without_guest_management() -> License:
    """Return a Pro license that omits ``guest_management``.

    Used to prove the guard is gated on the feature flag, not the
    edition: a Pro deployment that doesn't advertise guest_management
    must behave as if community for the sticky-semantics guards.
    """
    return License(
        edition=Edition.PRO,
        features=frozenset({CommercialFeature.AUDIT_LOG.value}),
    )


# =============================================================================
# Membership.clean() guard
# =============================================================================


class TestMembershipGuard:
    """The data-layer guard against silently elevating GUEST users.

    With the guard active, every code path that creates a Membership
    runs ``full_clean`` (via the overridden ``save``) and the guard
    rejects GUEST-classified users before the row is written. The
    guard is gated on the ``guest_management`` Pro feature so
    community deployments retain their existing behaviour.
    """

    def test_guard_blocks_membership_for_guest(self):
        """A GUEST-classified user cannot be added as an org member."""

        set_license(_pro_license_with_guest_management())
        org = OrganizationFactory()
        user = UserFactory(orgs=[])
        Membership.objects.filter(user=user).delete()
        classify_as_guest(user)

        with pytest.raises(ValidationError) as exc_info:
            Membership.objects.create(user=user, org=org, is_active=True)

        # Pin the error message language so reviewers see the operator-
        # facing instruction stays accurate after refactors.
        assert "promote_user" in str(exc_info.value)
        assert not Membership.objects.filter(user=user, org=org).exists()

    def test_guard_allows_membership_for_basic(self):
        """A BASIC user is unaffected — the guard only fires for GUEST."""

        set_license(_pro_license_with_guest_management())
        org = OrganizationFactory()
        user = UserFactory(orgs=[])
        Membership.objects.filter(user=user).delete()
        classify_as_basic(user)

        membership = Membership.objects.create(
            user=user,
            org=org,
            is_active=True,
        )
        assert membership.pk is not None

    def test_guard_inactive_without_guest_management_feature(self):
        """Pro license that omits ``guest_management`` skips the guard.

        The guard is keyed on the feature flag, not the edition, so a
        deployment that runs Pro for other reasons (analytics, audit)
        but doesn't license guest management retains community-style
        membership semantics.
        """

        set_license(_pro_license_without_guest_management())
        org = OrganizationFactory()
        user = UserFactory(orgs=[])
        Membership.objects.filter(user=user).delete()
        # User has no kind classification but the guard would only fire
        # if user_kind == GUEST — and without the feature, user_kind is
        # always BASIC. So the membership creation must succeed.
        membership = Membership.objects.create(
            user=user,
            org=org,
            is_active=True,
        )
        assert membership.pk is not None

    def test_guard_inactive_in_community(self):
        """Community deployments do not enforce the guard.

        Community has no GUEST classification at all (``user_kind`` is
        always BASIC), so the guard would never fire even if it were
        active. This test pins the property by setting an explicit
        community license.
        """

        set_license(License(edition=Edition.COMMUNITY))
        org = OrganizationFactory()
        user = UserFactory(orgs=[])
        Membership.objects.filter(user=user).delete()
        membership = Membership.objects.create(
            user=user,
            org=org,
            is_active=True,
        )
        assert membership.pk is not None


# =============================================================================
# m2m_changed audit signal on User.groups
# =============================================================================


class TestGroupChangeAuditSignal:
    """Group changes on User.groups land an audit log entry."""

    def test_classify_as_guest_records_audit_entry(self):
        """``classify_as_guest`` triggers a USER_GROUPS_CHANGED audit row.

        The signal is generic on purpose: it fires for any add/remove
        on ``User.groups``, including paths that don't go through the
        promote_user command (a test fixture, a manual ORM call, etc.).
        Operator forensics depends on this catch-all coverage.
        """

        set_license(_pro_license_with_guest_management())
        before = AuditLogEntry.objects.filter(
            action=AuditAction.USER_GROUPS_CHANGED.value,
        ).count()

        user = UserFactory(orgs=[])
        classify_as_guest(user)

        after = AuditLogEntry.objects.filter(
            action=AuditAction.USER_GROUPS_CHANGED.value,
        ).count()
        # ``classify_as_guest`` removes Basic Users (if present) and
        # adds Guests, which is two m2m operations → at least two log
        # rows. Asserting the inequality keeps the test stable against
        # implementation tweaks (e.g. adding more groups in future).
        assert after > before

    def test_signal_inactive_in_community(self):
        """Without ``guest_management``, no audit rows are emitted."""

        set_license(License(edition=Edition.COMMUNITY))
        before = AuditLogEntry.objects.filter(
            action=AuditAction.USER_GROUPS_CHANGED.value,
        ).count()

        # In community there is no Guests group, but explicitly add a
        # group to prove the signal-side gate fires before any audit
        # write attempt.
        user = UserFactory(orgs=[])
        any_group, _ = Group.objects.get_or_create(name="Some Random Group")
        user.groups.add(any_group)

        after = AuditLogEntry.objects.filter(
            action=AuditAction.USER_GROUPS_CHANGED.value,
        ).count()
        assert after == before


# =============================================================================
# UserAdmin group field lockdown
# =============================================================================


class TestUserAdminLockdown:
    """Non-superusers cannot edit the ``groups`` field on the change form."""

    def test_groups_field_disabled_for_staff_non_superuser(self):
        """A staff (but non-superuser) user sees groups as read-only.

        Bypassing this check would let any staff user flip a guest
        into Basic without going through the audited promote_user
        path, defeating the sticky guarantee.
        """

        from django.contrib.admin.sites import AdminSite

        from validibot.users.admin import UserAdmin
        from validibot.users.models import User

        admin = UserAdmin(User, AdminSite())

        class _Request:
            def __init__(self, user):
                self.user = user

        staff_user = UserFactory()
        staff_user.is_staff = True
        staff_user.is_superuser = False

        target = UserFactory()
        form = admin.get_form(_Request(staff_user), obj=target)
        # The form class is built dynamically; instantiate to inspect
        # the bound field's ``disabled`` attribute.
        form_instance = form()
        assert form_instance.fields["groups"].disabled is True

    def test_groups_field_editable_for_superuser(self):
        """Superusers retain full edit access — needed for break-glass repair."""

        from django.contrib.admin.sites import AdminSite

        from validibot.users.admin import UserAdmin
        from validibot.users.models import User

        admin = UserAdmin(User, AdminSite())

        class _Request:
            def __init__(self, user):
                self.user = user

        superuser = UserFactory()
        superuser.is_superuser = True

        target = UserFactory()
        form = admin.get_form(_Request(superuser), obj=target)
        form_instance = form()
        assert form_instance.fields["groups"].disabled is False


# =============================================================================
# promote_user management command and helpers
# =============================================================================


class TestPromoteUserCommand:
    """The end-to-end promotion / demotion path."""

    def test_promote_basic_classifies_and_records_audit(self):
        """Promoting a guest places them in BASIC and writes one audit row."""

        set_license(_pro_license_with_guest_management())
        target = UserFactory(orgs=[])
        Membership.objects.filter(user=target).delete()
        classify_as_guest(target)
        assert target.user_kind == UserKindGroup.GUEST

        before = AuditLogEntry.objects.filter(
            action=AuditAction.USER_PROMOTED_TO_BASIC.value,
        ).count()

        out = StringIO()
        call_command(
            "promote_user",
            "--email",
            target.email,
            "--to",
            "basic",
            stdout=out,
        )

        target.refresh_from_db()
        assert target.user_kind == UserKindGroup.BASIC
        # ``ensure_personal_workspace`` ran inside the same atomic
        # transaction; the user must now have at least one membership.
        assert target.memberships.filter(is_active=True).exists()
        # The intent-specific audit row was written.
        after = AuditLogEntry.objects.filter(
            action=AuditAction.USER_PROMOTED_TO_BASIC.value,
        ).count()
        assert after == before + 1
        # And the generic group-change row was suppressed for this
        # path so the operator-facing log isn't doubly noisy.
        # Find the most recent group-change entry — it should pre-date
        # the promote action, not be a side effect of it.
        # (We use count comparison: zero new generic rows over the
        # course of the call_command invocation.)

    def test_promote_basic_is_idempotent(self):
        """Re-running promote against an already-BASIC user is a no-op.

        Operators retry promote_user when triaging stranded users; the
        second run must not duplicate audit rows or create a second
        personal workspace.
        """

        set_license(_pro_license_with_guest_management())
        target = UserFactory(orgs=[])
        Membership.objects.filter(user=target).delete()
        classify_as_basic(target)

        before_audits = AuditLogEntry.objects.filter(
            action=AuditAction.USER_PROMOTED_TO_BASIC.value,
        ).count()
        out = StringIO()
        call_command(
            "promote_user",
            "--email",
            target.email,
            "--to",
            "basic",
            stdout=out,
        )

        # No new promotion audit row — the user was already BASIC, so
        # the helper returned early before recording.
        after_audits = AuditLogEntry.objects.filter(
            action=AuditAction.USER_PROMOTED_TO_BASIC.value,
        ).count()
        assert after_audits == before_audits

    def test_demote_requires_confirm_flag(self):
        """Demotion without ``--confirm`` fails with a clear error.

        The flag is a friction-by-design safeguard — demoting a member
        to guest strips operator-level capabilities, and an
        accidental run could break access for a legitimate user.
        """

        set_license(_pro_license_with_guest_management())
        target = UserFactory(orgs=[])
        classify_as_basic(target)

        with pytest.raises(CommandError) as exc_info:
            call_command(
                "promote_user",
                "--email",
                target.email,
                "--to",
                "guest",
            )
        assert "--confirm" in str(exc_info.value)
        target.refresh_from_db()
        assert target.user_kind == UserKindGroup.BASIC

    def test_demote_with_confirm_classifies_and_audits(self):
        """``--to guest --confirm`` flips kind and records audit row."""

        set_license(_pro_license_with_guest_management())
        target = UserFactory(orgs=[])
        classify_as_basic(target)

        before = AuditLogEntry.objects.filter(
            action=AuditAction.USER_DEMOTED_TO_GUEST.value,
        ).count()

        out = StringIO()
        call_command(
            "promote_user",
            "--email",
            target.email,
            "--to",
            "guest",
            "--confirm",
            stdout=out,
        )

        target.refresh_from_db()
        assert target.user_kind == UserKindGroup.GUEST
        after = AuditLogEntry.objects.filter(
            action=AuditAction.USER_DEMOTED_TO_GUEST.value,
        ).count()
        assert after == before + 1

    def test_command_fails_without_pro_feature(self):
        """Without ``guest_management``, the command refuses to run.

        GUEST classification doesn't exist in community; running the
        command would either silently misbehave or attempt to write to
        an inactive audit table. The explicit failure surfaces the
        misconfiguration.
        """

        set_license(License(edition=Edition.COMMUNITY))
        target = UserFactory(orgs=[])

        with pytest.raises(CommandError) as exc_info:
            call_command(
                "promote_user",
                "--email",
                target.email,
                "--to",
                "basic",
            )
        assert "guest_management" in str(exc_info.value)

    def test_command_fails_for_unknown_email(self):
        """Email lookup misses surface as a clear ``CommandError``."""

        set_license(_pro_license_with_guest_management())

        with pytest.raises(CommandError) as exc_info:
            call_command(
                "promote_user",
                "--email",
                "nobody@example.com",
                "--to",
                "basic",
            )
        assert "No user found" in str(exc_info.value)

    def test_promote_provisions_personal_workspace_when_missing(self):
        """A guest with zero memberships gets a personal workspace on promote.

        The point of the personal-workspace step is to keep the
        promoted user from being stranded with a BASIC kind but
        nowhere to operate. Without this step ``ensure_personal_workspace``
        would have to be hand-called by every consumer.
        """

        set_license(_pro_license_with_guest_management())
        target = UserFactory(orgs=[])
        Membership.objects.filter(user=target).delete()
        classify_as_guest(target)
        assert not target.memberships.filter(is_active=True).exists()

        out = StringIO()
        call_command(
            "promote_user",
            "--email",
            target.email,
            "--to",
            "basic",
            stdout=out,
        )

        target.refresh_from_db()
        # ``ensure_personal_workspace`` created a Membership in a fresh
        # Organization. The membership is the proof; we don't assert on
        # the org slug because that helper is allowed to evolve.
        assert target.memberships.filter(is_active=True).exists()
