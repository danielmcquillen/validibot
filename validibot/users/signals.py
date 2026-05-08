from __future__ import annotations

import contextlib
from contextvars import ContextVar

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models.signals import m2m_changed
from django.db.models.signals import post_save
from django.dispatch import receiver

from validibot.users.models import User
from validibot.users.models import ensure_personal_workspace


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_personal_workspace(sender, instance, created, **kwargs):
    if not created:
        return
    transaction.on_commit(lambda: ensure_personal_workspace(instance))


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def classify_new_user_as_basic(sender, instance, created, **kwargs):
    """Add newly-created users to the ``Basic Users`` classifier group.

    Every user belongs in exactly one of the two classifier groups
    (``Basic Users`` or ``Guests``). Existing users were placed by the
    data migration; new users land here. Guest invite acceptance flows
    reclassify into ``Guests`` when appropriate.

    Gated by the ``guest_management`` Pro feature: in community-only
    deployments every user's ``user_kind`` is BASIC by definition (no
    GUEST classification exists without Pro), so leaving the groups
    unpopulated is correct.
    """

    if not created:
        return

    # Local imports avoid a circular dependency between the users app and
    # the core feature/license machinery.
    from django.contrib.auth.models import Group

    from validibot.core.features import CommercialFeature
    from validibot.core.features import is_feature_enabled
    from validibot.users.constants import UserKindGroup

    if not is_feature_enabled(CommercialFeature.GUEST_MANAGEMENT):
        return

    def _classify():
        basic, _ = Group.objects.get_or_create(name=UserKindGroup.BASIC.value)
        instance.groups.add(basic)

    transaction.on_commit(_classify)


# Suppression flag used by ``promote_user`` and the matching admin
# action to skip the generic ``USER_GROUPS_CHANGED`` audit row when a
# more specific ``USER_PROMOTED_TO_BASIC`` / ``USER_DEMOTED_TO_GUEST``
# event has already been recorded by the caller. Without this the
# audit log would carry both rows for every promotion/demotion —
# noisy and confusing. Implemented as a :class:`ContextVar` so the
# state is correctly scoped to the current thread/coroutine and reset
# on context exit even if the wrapped code raises.
_suppress_group_change_audit_var: ContextVar[bool] = ContextVar(
    "suppress_group_change_audit",
    default=False,
)


@contextlib.contextmanager
def suppress_group_change_audit():
    """Context manager: skip the generic group-change audit record.

    Use when a higher-level operation (``promote_user``, the admin
    action wrapper) is already going to record its own intent-specific
    audit row and the generic m2m_changed-driven row would be a
    duplicate. ``ContextVar`` semantics mean the flag is per-thread /
    per-coroutine and reset on exit even if the wrapped code raises.
    """

    token = _suppress_group_change_audit_var.set(True)
    try:
        yield
    finally:
        _suppress_group_change_audit_var.reset(token)


@receiver(m2m_changed, sender=User.groups.through)
def audit_user_group_changes(sender, instance, action, pk_set, **kwargs):
    """Record an audit log entry whenever a user's group membership changes.

    Listens to both add and remove operations on ``User.groups`` so the
    audit log captures every classifier flip, whether driven by the
    sanctioned :func:`~validibot.users.user_kind.classify_as_guest` /
    :func:`~validibot.users.user_kind.classify_as_basic` helpers, by
    the ``promote_user`` command, by Django admin, or by an unexpected
    code path. Forensic visibility is the point — operators reviewing
    the audit log need to be able to trace any classification change
    back to its actor.

    Gated on the ``guest_management`` Pro feature so community
    deployments without ``audit_log`` infrastructure don't pay the
    cost. The audit module itself is also Pro-gated; recording into a
    non-existent audit table would be a hard failure.

    The ``post_add`` / ``post_remove`` actions fire after the m2m
    table is updated; ``pre_*`` actions are skipped to avoid double
    recording. The ``reverse=False`` direction is the only one that
    matters here (forward = ``user.groups.add(group)``); reverse
    (``group.user_set.add(user)``) also fires the same signal and is
    handled identically.
    """

    if action not in ("post_add", "post_remove"):
        return

    if _suppress_group_change_audit_var.get():
        return

    from validibot.core.features import CommercialFeature
    from validibot.core.features import is_feature_enabled

    if not is_feature_enabled(CommercialFeature.GUEST_MANAGEMENT):
        return

    if not pk_set:
        return

    # Local imports — the audit module is Pro-only and pulling it in
    # at module load would bind the import to community deployments
    # where it does nothing.
    from validibot.audit.constants import AuditAction
    from validibot.audit.services import ActorSpec
    from validibot.audit.services import AuditLogService

    group_names = list(
        Group.objects.filter(pk__in=pk_set).values_list("name", flat=True),
    )

    AuditLogService.record(
        action=AuditAction.USER_GROUPS_CHANGED,
        actor=ActorSpec(user=instance),
        target=instance,
        metadata={
            "operation": action,  # "post_add" or "post_remove"
            "group_names": group_names,
        },
    )
