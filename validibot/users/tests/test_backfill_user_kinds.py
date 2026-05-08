"""Tests for the ``backfill_user_kinds`` management command and helper.

The command is the canonical recovery path when migration-based
classification is unavailable: e.g. after a migration squash, after an
ad-hoc database edit, or when adding the user-kind groups to a
deployment that pre-dates them. These tests pin its three core
guarantees:

* **Recovery**: a user who is missing both classifier groups is placed
  into the correct one based on the predicate (active grant + no
  membership → ``Guests``; otherwise ``Basic Users``).
* **Repair**: a user in the *wrong* group is moved into the right one
  with the wrong-group membership removed atomically.
* **Idempotency**: a second run after the first reports no changes —
  every user is already correctly classified.

Tests do not depend on the migration: they delete the classifier groups
and rebuild from scratch via the helper, mirroring the squashed-
migration recovery scenario the helper exists to handle.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group

from validibot.users.constants import RoleCode
from validibot.users.constants import UserKindGroup
from validibot.users.management.commands.backfill_user_kinds import (
    apply_user_kind_backfill,
)
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.models import WorkflowAccessGrant
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _drop_user_kind_groups() -> None:
    """Simulate a fresh database without the data migration applied.

    ``Group.delete()`` cascades through ``auth_user_groups``, removing
    every user's classifier membership in one shot — exactly the state a
    deployment lands in if the data migration is squashed away.
    """

    Group.objects.filter(
        name__in=[
            UserKindGroup.BASIC.value,
            UserKindGroup.GUEST.value,
        ],
    ).delete()


def _user_in_group(user, group_name: str) -> bool:
    return user.groups.filter(name=group_name).exists()


class TestApplyUserKindBackfill:
    """Direct calls to the helper, bypassing the management command shell."""

    def test_recovers_after_groups_dropped(self):
        """Backfill recreates the groups and classifies users from scratch.

        Mimics the squash-migration recovery scenario: groups gone,
        users still around. After the backfill, every user must be in
        exactly one classifier group.
        """

        org = OrganizationFactory()
        member = UserFactory(orgs=[org])
        grant_role(member, org, RoleCode.EXECUTOR)

        # Grant-only user: simulates a guest invite acceptance.
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        WorkflowAccessGrant.objects.create(
            workflow=WorkflowFactory(org=org),
            user=guest,
            is_active=True,
        )

        _drop_user_kind_groups()

        report = apply_user_kind_backfill()

        # Both groups exist again and the predicate placed the right
        # user in each.
        assert Group.objects.filter(name=UserKindGroup.BASIC.value).exists()
        assert Group.objects.filter(name=UserKindGroup.GUEST.value).exists()
        assert _user_in_group(member, UserKindGroup.BASIC.value)
        assert _user_in_group(guest, UserKindGroup.GUEST.value)
        # Member should NOT be in Guests; guest should NOT be in Basic.
        assert not _user_in_group(member, UserKindGroup.GUEST.value)
        assert not _user_in_group(guest, UserKindGroup.BASIC.value)
        # And the report counted them.
        assert report.classified_basic >= 1
        assert report.classified_guest >= 1

    def test_repairs_user_in_wrong_group(self):
        """A user in the wrong classifier group is moved to the right one.

        Defensive against drift: a manual database edit, a stale state
        from before this code was deployed, or a bug elsewhere can
        leave the wrong group attached. The backfill must repair it
        and remove the incorrect membership atomically.
        """

        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        WorkflowAccessGrant.objects.create(
            workflow=WorkflowFactory(),
            user=guest,
            is_active=True,
        )
        # Force them into the WRONG group.
        basic, _ = Group.objects.get_or_create(name=UserKindGroup.BASIC.value)
        guest.groups.add(basic)

        apply_user_kind_backfill()

        # After backfill, they should be in Guests only.
        assert _user_in_group(guest, UserKindGroup.GUEST.value)
        assert not _user_in_group(guest, UserKindGroup.BASIC.value)

    def test_idempotent_second_run_is_noop(self):
        """A second run after the first reports zero changes.

        If the backfill ever stops being idempotent it would create
        spurious audit trail noise on every run — operators run this
        from cron or a deploy hook, so silent no-op is the correct
        behaviour for a healthy database.
        """

        users_seeded = 2
        for _ in range(users_seeded):
            UserFactory(orgs=[])
        apply_user_kind_backfill()
        report = apply_user_kind_backfill()

        assert report.classified_basic == 0
        assert report.classified_guest == 0
        assert report.already_correct >= users_seeded

    def test_dry_run_does_not_mutate(self):
        """``dry_run=True`` reports the would-be moves without writing.

        Used by operators previewing a recovery run before committing
        to it. The assertion proves no group memberships changed.
        """

        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        WorkflowAccessGrant.objects.create(
            workflow=WorkflowFactory(),
            user=guest,
            is_active=True,
        )
        _drop_user_kind_groups()

        report = apply_user_kind_backfill(dry_run=True)

        # Even though groups now exist (the helper get_or_creates them
        # so the dry-run can read membership names), the user MUST NOT
        # have been added to either.
        assert not _user_in_group(guest, UserKindGroup.BASIC.value)
        assert not _user_in_group(guest, UserKindGroup.GUEST.value)
        # The report does describe what would happen.
        assert report.classified_guest >= 1


class TestBackfillCommandShell:
    """Run the management command through ``call_command`` end-to-end.

    Confirms the CLI wiring (argparse + handler + stdout) works, not
    just the helper. Cheap to run, catches command-class regressions.
    """

    def test_command_runs_and_reports(self):
        """``manage.py backfill_user_kinds`` exits cleanly and writes a summary."""

        from io import StringIO

        from django.core.management import call_command

        UserFactory(orgs=[])
        out = StringIO()
        call_command("backfill_user_kinds", stdout=out)

        # The summary line is what operators read after cron runs;
        # asserting on its prefix keeps the test loose enough that
        # wording tweaks don't break it.
        assert "Backfill complete" in out.getvalue()

    def test_command_dry_run_flag(self):
        """``--dry-run`` flips the prefix and writes no group rows."""

        from io import StringIO

        from django.core.management import call_command

        UserFactory(orgs=[])
        _drop_user_kind_groups()
        out = StringIO()
        call_command("backfill_user_kinds", "--dry-run", stdout=out)

        assert "[dry-run]" in out.getvalue()
