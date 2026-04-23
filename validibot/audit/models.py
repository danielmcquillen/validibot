"""Audit log schema — two-layer actor/event split for GDPR-friendly erasure.

The schema follows ADR-2026-04-16 §2. Two tables rather than one:

* ``AuditActor`` — identifies *who* took the action (user FK, email,
  ip, user-agent). Purgeable: on an erasure request we null the PII
  fields and set ``erased_at`` so the actor row remains linkable from
  ``AuditLogEntry`` FKs but no longer resolves to a named person.
  This satisfies GDPR Recital 26 pseudonymisation.
* ``AuditLogEntry`` — describes *what* happened (action, target,
  whitelisted field diff, free-form metadata). Append-only in the
  ordinary flow; the only permitted mutation is the erasure-sanitisation
  path described in the runbook (and itself audited via
  ``AUDIT_ENTRY_SANITISED``).

Both tables live in the community repo so self-hosted Pro deployments
get audit logs. The UI that surfaces these entries will be Pro-gated
(Phase 2).
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from validibot.audit.constants import AuditAction
from validibot.users.models import Organization
from validibot.users.models import User


class AuditActor(models.Model):
    """Identity layer — purgeable for right-to-erasure.

    Split from ``AuditLogEntry`` so a user's PII can be nulled without
    destroying the audit trail. The ``AuditLogEntry.actor`` foreign key
    uses ``PROTECT`` to ensure no actor row is ever dropped while
    entries still reference it.
    """

    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_actors",
        help_text=_(
            "The Django user who took the action. NULL for system-originated "
            "entries (management commands, signal handlers) or after the "
            "user row itself is purged.",
        ),
    )
    # Denormalised email/IP/UA rather than joined via ``user`` because
    # users can be deleted entirely, and the audit trail should survive
    # that deletion. Nulled during erasure — the ``erased_at`` flag
    # distinguishes "actor never had an email" from "email was nulled
    # during an erasure request".
    # DJ001 waived: we deliberately distinguish NULL ("email was erased")
    # from empty string ("anonymous actor never had an email") so the
    # Pro UI can render the two states differently.
    email = models.EmailField(  # noqa: DJ001
        null=True,
        blank=True,
        help_text=_(
            "Actor email captured at action time. Nulled during GDPR "
            "erasure; the ``erased_at`` timestamp distinguishes the two.",
        ),
    )
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text=_(
            "IP the action was taken from. Under APP 11 / GDPR treated as "
            "personal info when combined with other identifiers, so it is "
            "cleared by the erasure workflow.",
        ),
    )
    user_agent = models.TextField(
        blank=True,
        default="",
        help_text=_("HTTP User-Agent header at action time."),
    )
    erased_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_(
            "Timestamp when this actor's PII was nulled. NULL means the "
            "actor still resolves to a named person.",
        ),
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    class Meta:
        verbose_name = _("Audit Actor")
        verbose_name_plural = _("Audit Actors")
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["email"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.erased_at is not None:
            return f"AuditActor #{self.pk} (erased)"
        if self.email:
            return f"AuditActor #{self.pk} <{self.email}>"
        return f"AuditActor #{self.pk}"


class AuditLogEntry(models.Model):
    """Event layer — append-only, immutable in the ordinary flow.

    Every entry references an ``AuditActor`` (who), optionally an
    ``Organization`` (scope), and records an ``action`` against a
    target identified by ``(target_type, target_id)`` — string-keyed so
    entries survive the deletion of their target object.
    """

    actor = models.ForeignKey(
        AuditActor,
        on_delete=models.PROTECT,
        related_name="log_entries",
        help_text=_(
            "PROTECT rather than CASCADE: deleting an actor should be "
            "rejected at the DB level. Actor rows are nulled via the "
            "erasure workflow, never removed.",
        ),
    )
    org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_log_entries",
        help_text=_(
            "Scope of the event. NULL for global/system-level actions "
            "(e.g. superuser admin changes outside an org context).",
        ),
    )
    action = models.CharField(
        max_length=64,
        choices=AuditAction.choices,
        help_text=_("Machine-readable action code; see AuditAction."),
    )
    # String-keyed target so deleting the target row does not orphan
    # the audit entry. ``target_repr`` snapshots a human label at action
    # time so reports remain readable when the target object is gone.
    target_type = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_(
            "Django model label of the target, e.g. ``workflows.Workflow``. "
            "Empty for actions without a target (e.g. LOGIN_FAILED).",
        ),
    )
    target_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_(
            "String primary key of the target. Stored as string (not FK) "
            "so the entry survives target deletion.",
        ),
    )
    target_repr = models.CharField(
        max_length=256,
        blank=True,
        default="",
        help_text=_(
            "Human-readable label captured at action time — survives target deletion.",
        ),
    )
    changes = models.JSONField(
        null=True,
        blank=True,
        help_text=_(
            "Whitelisted before/after field snapshot: "
            "``{field: {'before': ..., 'after': ...}}``. Fields outside "
            "``AUDITABLE_FIELDS`` are recorded as ``<redacted>``.",
        ),
    )
    metadata = models.JSONField(
        null=True,
        blank=True,
        help_text=_(
            "Free-form, sanitised supplementary context. Never stores "
            "secrets, full request bodies, or validation payloads.",
        ),
    )
    request_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_(
            "UUID per request, set by ``AuditContextMiddleware``. Used "
            "to correlate a DB entry back to Cloud Logging markers.",
        ),
    )
    occurred_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text=_("Wall-clock time the entry was written."),
    )

    class Meta:
        verbose_name = _("Audit Log Entry")
        verbose_name_plural = _("Audit Log Entries")
        indexes = [
            models.Index(fields=["org", "-occurred_at"]),
            models.Index(fields=["actor", "-occurred_at"]),
            models.Index(fields=["action", "-occurred_at"]),
            models.Index(fields=["target_type", "target_id"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"AuditLogEntry #{self.pk}: {self.action}"


# Keep TimeStampedModel available for tests that want ``updated``/``modified``
# without importing the third-party package directly. Not used on the
# audit models themselves — entries are immutable once written, so the
# ``occurred_at`` ``auto_now_add`` is all we need.
__all__ = [
    "AuditActor",
    "AuditLogEntry",
    "TimeStampedModel",
]
