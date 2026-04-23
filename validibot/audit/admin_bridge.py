"""Mirror Django admin actions into the audit log.

Django's admin UI records every Add/Change/Delete as a row in
``django.contrib.admin.LogEntry``. That's a proper audit trail in its
own right, but it lives in a separate table with its own retention
policy, and the Pro UI for the audit log (Phase 2) is already going
to have to query ``AuditLogEntry`` — forcing Pro users to also learn
the admin LogEntry table would be a UX own-goal.

This bridge keeps a shadow copy in ``AuditLogEntry`` with the action
code ``ADMIN_OBJECT_CHANGED``. The original ``admin.LogEntry`` row
stays untouched; the shadow is additive. That matters because the
admin log is what Django's built-in history view renders, and we
don't want to interfere with its semantics.

Why not replace admin.LogEntry entirely:

1. The Django admin history view queries LogEntry directly and
   expects certain columns (action_time, object_repr, change_message).
   Removing LogEntry would break history for every staff user.
2. Staff users often have no ``AuditLogEntry`` visibility (Pro UI is
   gated by ``AUDIT_LOG``) so the admin would lose its own audit
   trail.
3. Mirroring is one-way and append-only, which makes the bridge
   trivially safe — nothing here can corrupt either log.

Admin changes land in ``AuditLogEntry`` with:

* ``action`` = ``ADMIN_OBJECT_CHANGED`` regardless of whether the
  underlying admin action was add/change/delete. The distinction
  lives in ``metadata["admin_action"]`` (``"added"``/``"changed"``/
  ``"deleted"``). This keeps the audit-action taxonomy narrow —
  adding ``ADMIN_OBJECT_ADDED`` / ``ADMIN_OBJECT_DELETED`` /
  ``ADMIN_OBJECT_CHANGED`` triples every admin event.
* ``target_type`` = ``{app_label}.{model}`` from the LogEntry's
  ``ContentType``.
* ``target_id`` = the admin LogEntry's ``object_id``.
* ``target_repr`` = the admin LogEntry's ``object_repr`` snapshot.
* ``metadata.change_message`` = the admin's human-readable change
  summary (includes formset-level changes the plain model signals
  miss, like inline edits).

What we don't capture from LogEntry:
* The full ``change_message`` structure — admin encodes it as a
  JSON blob that may include formset labels we haven't whitelisted.
  We preserve the formatted string (``get_change_message()``) rather
  than the raw JSON so no unexpected PII sneaks through.
"""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.admin.models import ADDITION
from django.contrib.admin.models import CHANGE
from django.contrib.admin.models import DELETION
from django.contrib.admin.models import LogEntry
from django.db.models.signals import post_save

from validibot.audit.constants import AuditAction
from validibot.audit.context import get_current_context
from validibot.audit.services import ActorSpec
from validibot.audit.services import AuditLogService

logger = logging.getLogger(__name__)


_ADMIN_ACTION_LABELS: dict[int, str] = {
    ADDITION: "added",
    CHANGE: "changed",
    DELETION: "deleted",
}


def on_admin_log_entry_created(
    sender: type[LogEntry],
    instance: LogEntry,
    created: bool,  # noqa: FBT001  Django signal signature — positional bool is required.
    **kwargs: Any,
) -> None:
    """Mirror a freshly-created ``admin.LogEntry`` into our audit log.

    Only creates matter — the admin UI never updates an existing
    LogEntry, so a signal with ``created=False`` would be a weird
    custom save and we ignore it to avoid double-counting.
    """

    if not created:
        return

    # Admin actions always have a user attached (Django's
    # ``ModelAdmin.log_addition`` / ``log_change`` / ``log_deletion``
    # populate it from ``request.user``). Defensive check anyway —
    # a programmatic ``LogEntry.objects.create(...)`` might pass
    # ``user=None`` and we don't want to crash on that.
    user = instance.user_id and instance.user  # lazy resolve; avoid query if no id

    context = get_current_context()
    admin_action = _ADMIN_ACTION_LABELS.get(instance.action_flag, "unknown")

    # Content type may be None for custom admin log entries. Fall back
    # to the raw string representation rather than raising.
    content_type = instance.content_type
    if content_type is not None:
        target_type = f"{content_type.app_label}.{content_type.model}"
    else:
        target_type = ""

    AuditLogService.record(
        action=AuditAction.ADMIN_OBJECT_CHANGED,
        actor=ActorSpec(
            user=user or None,
            email=getattr(user, "email", None) if user else None,
            ip_address=context.actor.ip_address,
            user_agent=context.actor.user_agent,
        ),
        target_type=target_type,
        target_id=str(instance.object_id or ""),
        target_repr=instance.object_repr or "",
        request_id=context.request_id,
        metadata={
            "admin_action": admin_action,
            "change_message": instance.get_change_message(),
        },
    )


def connect_admin_bridge() -> None:
    """Attach the bridge to ``admin.LogEntry`` post-save.

    ``dispatch_uid`` guards against double-registration — same pattern
    as the other audit signals.
    """

    post_save.connect(
        on_admin_log_entry_created,
        sender=LogEntry,
        dispatch_uid="validibot_audit.admin_bridge.on_admin_log_entry_created",
    )
