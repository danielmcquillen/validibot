"""Single write path into the audit log.

All code that wants to record an audit event goes through
``AuditLogService.record()``. The service enforces three invariants:

1. **Field whitelisting** — anything not in ``AUDITABLE_FIELDS`` for
   the target model is recorded as ``<redacted>``. Protects against an
   accidental capture of an ``APIKey.key`` or a ``User.password`` hash.
2. **Actor resolution** — callers pass either a Django user or an
   explicit ``actor_kwargs`` dict; the service creates or reuses the
   corresponding ``AuditActor`` row. Phase 2 will replace the explicit
   dict with a thread-local set by ``AuditContextMiddleware``.
3. **Structured log marker** — every successful audit write also emits
   a minimal JSON line to stdout. Captured by Cloud Logging so the
   audit trail remains observable even if the DB write succeeds but
   the DB is later restored from an earlier backup.

The service is write-only in Phase 1. Phase 2 adds Pro-gated reads via
``views.py``; Phase 3 adds the erasure-sanitisation path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from django.db import models
from django.db import transaction

from validibot.audit.constants import AUDITABLE_FIELDS
from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditActor
from validibot.audit.models import AuditLogEntry

logger = logging.getLogger(__name__)

# Sentinel used in ``changes`` payloads when a non-whitelisted field
# changes. Callers see the fact of the change without the value.
REDACTED = "<redacted>"


@dataclass(frozen=True)
class ActorSpec:
    """Describe the actor responsible for an audit event.

    One of ``user`` or (``email``) is required. Phase 1 callers
    construct these inline; Phase 2's middleware will build them from
    the current request.
    """

    user: models.Model | None = None
    email: str | None = None
    ip_address: str | None = None
    user_agent: str = ""


class AuditLogService:
    """Write-only facade over the audit tables.

    Instance methods are avoided — every call is a single atomic
    operation that needs no shared state. Classmethods keep call sites
    short (``AuditLogService.record(...)``).
    """

    @classmethod
    def record(
        cls,
        *,
        action: AuditAction,
        actor: ActorSpec,
        org: models.Model | None = None,
        target: models.Model | None = None,
        target_type: str = "",
        target_id: str = "",
        target_repr: str = "",
        changes: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> AuditLogEntry:
        """Persist an audit entry and emit the Cloud Logging marker.

        One of ``target`` or the explicit ``target_{type,id,repr}``
        triple may be supplied. When both are given, the explicit values
        win (useful for recording actions against already-deleted
        targets during the erasure workflow).

        ``changes`` is auto-sanitised against ``AUDITABLE_FIELDS`` for
        the target's model — no caller needs to know the whitelist.
        """

        resolved_target_type, resolved_target_id, resolved_target_repr = (
            cls._resolve_target(
                target,
                target_type,
                target_id,
                target_repr,
            )
        )

        sanitised_changes = cls._sanitise_changes(
            target_type=resolved_target_type,
            changes=changes,
        )

        with transaction.atomic():
            actor_row = cls._get_or_create_actor(actor)
            entry = AuditLogEntry.objects.create(
                actor=actor_row,
                org=org,
                action=action.value,
                target_type=resolved_target_type,
                target_id=resolved_target_id,
                target_repr=resolved_target_repr,
                changes=sanitised_changes,
                metadata=metadata or None,
                request_id=request_id,
            )

        cls._emit_log_marker(entry)
        return entry

    # ── helpers ─────────────────────────────────────────────────────

    @classmethod
    def _get_or_create_actor(cls, spec: ActorSpec) -> AuditActor:
        """Create an ``AuditActor`` row for this request.

        We never reuse prior actor rows — each call creates a fresh row.
        That keeps the model simple (no "actor identity over time"
        concept) and means the erasure workflow can operate on entire
        actor rows without caring about which entries touched which
        session.
        """

        return AuditActor.objects.create(
            user=spec.user,
            email=spec.email
            or (getattr(spec.user, "email", None) if spec.user else None),
            ip_address=spec.ip_address,
            user_agent=spec.user_agent,
        )

    @staticmethod
    def _resolve_target(
        target: models.Model | None,
        target_type: str,
        target_id: str,
        target_repr: str,
    ) -> tuple[str, str, str]:
        """Derive ``(type, id, repr)`` from either a model or explicit args.

        Explicit args win when both are provided so erasure-time code
        can record actions against already-deleted targets.
        """

        resolved_type = target_type or (
            target._meta.label if target is not None else ""
        )
        resolved_id = target_id or (
            str(target.pk) if target is not None and target.pk is not None else ""
        )
        resolved_repr = target_repr or (str(target) if target is not None else "")
        return resolved_type, resolved_id, resolved_repr

    @staticmethod
    def _sanitise_changes(
        *,
        target_type: str,
        changes: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Replace non-whitelisted fields in a changes dict with ``<redacted>``.

        No target type → no whitelist lookup, no sanitisation. That's
        fine because the only entries without a target type are
        security events (LOGIN_FAILED etc.) where ``changes`` is not
        used.
        """

        if not changes:
            return None
        if not target_type:
            return dict(changes)

        allowed = set(AUDITABLE_FIELDS.get(target_type, ()))
        sanitised: dict[str, Any] = {}
        for field_name, value in changes.items():
            if field_name in allowed:
                sanitised[field_name] = value
            else:
                sanitised[field_name] = REDACTED
        return sanitised

    @staticmethod
    def _emit_log_marker(entry: AuditLogEntry) -> None:
        """Emit the minimal Cloud Logging marker for an audit write.

        Explicitly does NOT include PII — no email, no IP, no diff.
        The marker is enough to trace an entry back to the DB row and
        to fire Log-based Metrics (LOGIN_FAILED bursts etc.) in Phase 2.
        """

        logger.info(
            "audit_entry",
            extra={
                "audit_id": entry.pk,
                "action": entry.action,
                "org_id": entry.org_id,
                "target_type": entry.target_type,
                "target_id": entry.target_id,
                "request_id": entry.request_id,
                "occurred_at": entry.occurred_at.isoformat()
                if entry.occurred_at is not None
                else None,
            },
        )
        # Also emit a compact JSON line in case the running logger is
        # not configured with a JSON formatter. Cloud Logging scrapes
        # stdout on Cloud Run so this is the cheapest way to guarantee
        # the marker lands in the logging bucket.
        print(  # noqa: T201 — intentional structured output
            json.dumps(
                {
                    "event": "audit_entry",
                    "audit_id": entry.pk,
                    "action": entry.action,
                    "org_id": entry.org_id,
                    "target_type": entry.target_type,
                    "target_id": entry.target_id,
                    "request_id": entry.request_id,
                },
                separators=(",", ":"),
                default=str,
            ),
        )
