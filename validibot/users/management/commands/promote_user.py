"""Promote or demote a user between the ``Basic Users`` and ``Guests`` kinds.

The sanctioned, atomic, audited path for changing an account's
system-wide :class:`~validibot.users.constants.UserKindGroup`. A single
transaction does:

1. Flip the user's classifier group (``classify_as_basic`` or
   ``classify_as_guest``).
2. For ``--to basic``: ensure the user has at least one active
   ``Membership``. If they have none (e.g. a former guest with grants
   but no org seat), provision a personal workspace via
   :func:`~validibot.users.models.ensure_personal_workspace` so the
   promoted account isn't stranded with no org context.
3. Record a single ``USER_PROMOTED_TO_BASIC`` or
   ``USER_DEMOTED_TO_GUEST`` audit log row identifying the operator
   actor, the target user, and the kind change. The generic
   ``USER_GROUPS_CHANGED`` row that the m2m_changed signal would
   otherwise emit is suppressed for this code path so the audit log
   shows one clear "operator did X" record per promotion event.

Direction safety: promotions to BASIC are routine; demotions to GUEST
remove operator-level capabilities and require an explicit
``--confirm`` flag so a typo cannot accidentally strip member
authority. Demotion does NOT remove existing memberships — the
operator must clean those up separately if they want to fully end the
user's membership access. This is by design: a half-finished demotion
is recoverable; a destructive cascade is not.

Usage::

    # Promote a guest user to basic (the common case)
    python manage.py promote_user --email guest@example.com --to basic

    # Demote a basic user to guest (requires --confirm)
    python manage.py promote_user --email user@example.com --to guest --confirm

The command also checks for the ``guest_management`` Pro feature.
Without Pro, the GUEST classification doesn't exist; the command
exits with a clear error rather than silently misbehaving.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction

from validibot.users.constants import UserKindGroup

User = get_user_model()


def promote_user_to_basic(*, target, actor, request_id: str = ""):
    """Run the BASIC promotion path: classify + ensure org + audit row.

    Atomic: if the personal-org provisioning fails the classification
    flip is rolled back too, leaving the user untouched. The single
    audit row is recorded inside the transaction as well so a partial
    success can never leak a misleading log entry.

    Returns the target user; the caller decides what to print.
    """

    from validibot.audit.constants import AuditAction
    from validibot.audit.services import ActorSpec
    from validibot.audit.services import AuditLogService
    from validibot.users.models import ensure_personal_workspace
    from validibot.users.signals import suppress_group_change_audit
    from validibot.users.user_kind import classify_as_basic

    previous_kind = target.user_kind
    if previous_kind == UserKindGroup.BASIC:
        # Idempotent no-op: still ensure they have an org so a stranded
        # user can be repaired by re-running with no harm done. Use
        # ``force=True`` because a user who is already BASIC but holds
        # active grants and no memberships would otherwise hit the
        # legacy "has grants and no memberships → return None"
        # predicate and stay stranded.
        ensure_personal_workspace(target, force=True)
        return target

    with transaction.atomic():
        with suppress_group_change_audit():
            classify_as_basic(target)
        # Personal-org provisioning with ``force=True``: at this point
        # the user has just transitioned GUEST → BASIC, and any active
        # grants they hold are exactly the canonical pre-promotion
        # guest shape that the legacy predicate inside the helper would
        # short-circuit on. Forcing through the predicate guarantees a
        # workspace gets created in the same atomic transaction so the
        # promoted user always has somewhere to operate.
        ensure_personal_workspace(target, force=True)

        AuditLogService.record(
            action=AuditAction.USER_PROMOTED_TO_BASIC,
            actor=ActorSpec(user=actor) if actor else ActorSpec(email=""),
            target=target,
            metadata={
                "previous_kind": previous_kind.value,
                "new_kind": UserKindGroup.BASIC.value,
            },
            request_id=request_id,
        )

    return target


def demote_user_to_guest(*, target, actor, request_id: str = ""):
    """Run the GUEST demotion path: classify + audit row.

    Does NOT touch existing ``Membership`` rows — the demoted user
    keeps any org memberships they had until an operator removes
    them. The follow-up step matters because the
    :class:`~validibot.users.models.Membership.clean` guard prevents
    NEW memberships from being created for a GUEST user but does not
    retroactively invalidate existing rows. Cleanup of stale
    memberships is the operator's responsibility and a separate
    auditable event when it happens.
    """

    from validibot.audit.constants import AuditAction
    from validibot.audit.services import ActorSpec
    from validibot.audit.services import AuditLogService
    from validibot.users.signals import suppress_group_change_audit
    from validibot.users.user_kind import classify_as_guest

    previous_kind = target.user_kind
    if previous_kind == UserKindGroup.GUEST:
        return target

    with transaction.atomic():
        with suppress_group_change_audit():
            classify_as_guest(target)

        AuditLogService.record(
            action=AuditAction.USER_DEMOTED_TO_GUEST,
            actor=ActorSpec(user=actor) if actor else ActorSpec(email=""),
            target=target,
            metadata={
                "previous_kind": previous_kind.value,
                "new_kind": UserKindGroup.GUEST.value,
            },
            request_id=request_id,
        )

    return target


class Command(BaseCommand):
    """Move a user between the BASIC and GUEST classifier kinds."""

    help = (
        "Promote a user to BASIC (or demote to GUEST with --confirm). "
        "BASIC promotion ensures a personal workspace exists. Both paths "
        "record an audit log entry."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            required=True,
            help="Email address of the target user.",
        )
        parser.add_argument(
            "--to",
            choices=("basic", "guest"),
            required=True,
            help="Target kind: 'basic' (promote) or 'guest' (demote).",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help=(
                "Required for demotion (--to guest). Demotion strips "
                "member-level capabilities; the explicit flag prevents "
                "typos from accidentally removing access."
            ),
        )

    def handle(self, *args, **options):
        from validibot.core.features import CommercialFeature
        from validibot.core.features import is_feature_enabled

        if not is_feature_enabled(CommercialFeature.GUEST_MANAGEMENT):
            raise CommandError(
                "promote_user requires the 'guest_management' Pro feature. "
                "Install validibot-pro to use this command.",
            )

        email = options["email"]
        target_kind = options["to"]
        confirmed = options["confirm"]

        try:
            target = User.objects.get(email__iexact=email)
        except User.DoesNotExist as exc:
            raise CommandError(f"No user found with email {email!r}.") from exc

        if target_kind == "guest" and not confirmed:
            raise CommandError(
                "Demotion to GUEST requires --confirm. Re-run with that flag "
                "if this is intentional.",
            )

        if target_kind == "basic":
            promote_user_to_basic(target=target, actor=None)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Promoted {target.email} to BASIC.",
                ),
            )
        else:
            demote_user_to_guest(target=target, actor=None)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Demoted {target.email} to GUEST.",
                ),
            )
