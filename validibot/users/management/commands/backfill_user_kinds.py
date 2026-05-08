"""Idempotent re-classification of every user into a user-kind group.

The data migration ``users/0004_user_kind_groups`` performs this same
backfill on first deploy. This command exists for the cases where
relying on a migration alone is risky:

* Migrations are squashed — replaying the squashed file does not re-run
  ``RunPython`` operations against existing rows; the squashed
  migration would only execute on a fresh database. Running this
  command after a squash restores classification on existing
  installations.
* A future schema change adds new state (e.g. an account flag) that
  the original predicate would have classified differently. Updating
  the helper below and running this command resyncs without writing a
  new migration.
* A bug or manual database edit leaves users without a classifier
  group. Running this command repairs them.

The predicate: a user is classified ``Guests`` iff they have at least
one active ``WorkflowAccessGrant`` and no active ``Membership``;
everyone else lands in ``Basic Users``. This is the intuitive "user
operates only as a workflow guest" condition at the moment the backfill
runs — once classified, the kind is sticky and only flips via
``classify_as_guest`` / ``classify_as_basic`` (or the audited
``promote_user`` command). The helper :func:`apply_user_kind_backfill`
is exposed as a module-level function so tests can call it without
shelling out to the management command.

Usage::

    # Re-classify every user
    python manage.py backfill_user_kinds

    # Preview changes without writing
    python manage.py backfill_user_kinds --dry-run

Idempotent — safe to re-run. Reports counts of users moved into each
group plus the count of users who needed no change.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction

from validibot.users.constants import UserKindGroup
from validibot.users.models import User


@dataclass(frozen=True)
class BackfillReport:
    """Summary of what :func:`apply_user_kind_backfill` did or would do."""

    classified_basic: int
    classified_guest: int
    already_correct: int

    @property
    def changed(self) -> int:
        return self.classified_basic + self.classified_guest

    @property
    def total(self) -> int:
        return self.changed + self.already_correct


def apply_user_kind_backfill(*, dry_run: bool = False) -> BackfillReport:
    """Classify every user into ``Basic Users`` or ``Guests``.

    Returns a :class:`BackfillReport` with the move counts. With
    ``dry_run=True``, the report describes the changes that *would* be
    made; no group memberships are mutated.

    The function ensures both classifier groups exist (``get_or_create``)
    so it works as a recovery tool even if the original migration was
    squashed away.
    """

    from validibot.workflows.models import WorkflowAccessGrant

    basic_group, _ = Group.objects.get_or_create(name=UserKindGroup.BASIC.value)
    guest_group, _ = Group.objects.get_or_create(name=UserKindGroup.GUEST.value)

    # Guest predicate: active grant AND no active membership.
    # Captures the "this account currently operates only as a workflow
    # guest" intent so an operator-driven re-run lands every user in
    # the right classifier without manual triage.
    guest_user_ids = set(
        WorkflowAccessGrant.objects.filter(is_active=True)
        .exclude(user__memberships__is_active=True)
        .values_list("user_id", flat=True),
    )

    classified_basic = 0
    classified_guest = 0
    already_correct = 0

    # ``transaction.atomic`` brackets the writes so a failure mid-loop
    # leaves the database in its prior state. The dry-run path skips
    # the atomic block since no writes happen.
    if dry_run:
        for user in User.objects.iterator():
            target = guest_group if user.id in guest_user_ids else basic_group
            other = basic_group if target is guest_group else guest_group
            user_group_names = set(user.groups.values_list("name", flat=True))
            in_target = target.name in user_group_names
            in_other = other.name in user_group_names
            if in_target and not in_other:
                already_correct += 1
            elif target is guest_group:
                classified_guest += 1
            else:
                classified_basic += 1
        return BackfillReport(
            classified_basic=classified_basic,
            classified_guest=classified_guest,
            already_correct=already_correct,
        )

    with transaction.atomic():
        for user in User.objects.iterator():
            target = guest_group if user.id in guest_user_ids else basic_group
            other = basic_group if target is guest_group else guest_group
            user_group_names = set(user.groups.values_list("name", flat=True))
            in_target = target.name in user_group_names
            in_other = other.name in user_group_names

            if in_target and not in_other:
                already_correct += 1
                continue

            if in_other:
                user.groups.remove(other)
            if not in_target:
                user.groups.add(target)
            if target is guest_group:
                classified_guest += 1
            else:
                classified_basic += 1

    return BackfillReport(
        classified_basic=classified_basic,
        classified_guest=classified_guest,
        already_correct=already_correct,
    )


class Command(BaseCommand):
    """Re-classify every user into the right user-kind group."""

    help = (
        "Idempotently classify every user into 'Basic Users' or 'Guests' "
        "based on workflow grants and active memberships. Safe to re-run; "
        "use --dry-run to preview."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without modifying the database.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        report = apply_user_kind_backfill(dry_run=dry_run)

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Backfill complete: "
                f"{report.classified_basic} → Basic Users, "
                f"{report.classified_guest} → Guests, "
                f"{report.already_correct} already correct "
                f"({report.total} total).",
            ),
        )
