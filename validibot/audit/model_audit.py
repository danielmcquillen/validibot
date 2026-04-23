"""Generic model-level audit capture driven by a small registry.

Each auditable model registers which ``AuditAction`` to use for its
create/update/delete lifecycle. A single trio of signal receivers
(``_on_pre_save``, ``_on_post_save``, ``_on_pre_delete``) dispatches to
the registry so we don't end up with a copy-pasted receiver per model.

Design notes:

1. **Diff computation uses the whitelist**. The snapshot function
   captures only fields listed in ``AUDITABLE_FIELDS`` for the model's
   ``Meta.label``. Everything else is invisible to the audit layer —
   so a diff on a workflow that only touched ``internal_notes`` (not
   whitelisted) produces an empty changes dict and is silently dropped.
   That is *correct*: recording "something changed" with no visible
   fields adds noise without adding forensic value.

2. **Pre-save snapshot lives on the instance**, not a global dict.
   Attaching ``_audit_pre_save_snapshot`` to the instance means each
   save() only keeps one snapshot in memory and it is gc'd with the
   instance. It also avoids any concurrency questions — concurrent
   saves on different instances never touch the same slot.

3. **Create/delete detection**. ``pre_save`` only snapshots when
   ``instance.pk`` is already set (an update). ``post_save`` reads
   ``created`` directly from the signal kwargs. ``pre_delete`` captures
   the instance *before* the row is gone; there's no matching post
   hook because the service only needs the target info the instance
   still carries.

4. **No action → no audit**. A model registered only for "update" can
   still be created/deleted without noise — the receivers check whether
   an action is registered for each event and short-circuit if not.

Phase-2 will add:

* An `admin.LogEntry` bridge to capture admin-initiated changes
  without every-model registration (``register_model_audit`` is
  still needed for typed ``changes`` diffs on the direct-save path).
* Through-table hooks for M2M relations like ``MembershipRole`` so
  role grants land on MEMBER_ROLE_CHANGED.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from django.db.models.signals import post_save
from django.db.models.signals import pre_delete
from django.db.models.signals import pre_save

from validibot.audit.constants import AUDITABLE_FIELDS
from validibot.audit.constants import AuditAction
from validibot.audit.context import get_current_context
from validibot.audit.services import AuditLogService

if TYPE_CHECKING:
    from django.db.models import Model

logger = logging.getLogger(__name__)

# Sentinel name for the snapshot slot we attach to model instances.
# Starts with an underscore so Django's ``Model`` __setattr__ accepts
# it without treating it as a field.
_PRE_SAVE_SLOT = "_audit_pre_save_snapshot"


@dataclass(frozen=True)
class AuditActionTriplet:
    """Which actions to record for a model's lifecycle events.

    Any field can be ``None`` — a model that should only audit updates
    (e.g. we track role changes but not initial creation) leaves
    ``create`` and ``delete`` unset and only fills ``update``.
    """

    create: AuditAction | None = None
    update: AuditAction | None = None
    delete: AuditAction | None = None


class ModelAuditRegistry:
    """Map ``model._meta.label`` → ``AuditActionTriplet``.

    Registrations happen during app ``ready()`` so the dispatch table
    is ready before any save/delete signal could fire. Re-registration
    of the same model is allowed; the last registration wins.
    """

    def __init__(self) -> None:
        self._map: dict[str, AuditActionTriplet] = {}

    def register(
        self,
        model: type[Model],
        *,
        create: AuditAction | None = None,
        update: AuditAction | None = None,
        delete: AuditAction | None = None,
    ) -> None:
        """Record which actions to use for ``model`` lifecycle events."""

        self._map[model._meta.label] = AuditActionTriplet(
            create=create,
            update=update,
            delete=delete,
        )

    def unregister(self, model: type[Model]) -> None:
        """Remove a registration. Intended for test cleanup."""

        self._map.pop(model._meta.label, None)

    def actions_for(self, model_label: str) -> AuditActionTriplet | None:
        """Return the triplet for ``model_label`` or ``None`` if unknown."""

        return self._map.get(model_label)

    def is_audited(self, model_label: str) -> bool:
        """Is this model known to the audit layer at all?"""

        return model_label in self._map

    def registered_labels(self) -> list[str]:
        """Snapshot of current registrations — handy in tests."""

        return sorted(self._map.keys())


# Singleton used by the signal receivers below.
model_audit_registry = ModelAuditRegistry()


# ── snapshots + diffs ───────────────────────────────────────────────


def _snapshot_auditable_fields(instance: Model) -> dict[str, Any]:
    """Return ``{field: value}`` for whitelisted fields on ``instance``.

    Looks up ``AUDITABLE_FIELDS[instance._meta.label]`` and reads each
    named attribute off the instance. Missing fields (for whatever
    reason — whitelist drift, model refactor) are silently skipped so
    the audit layer fails open: if the whitelist goes stale we capture
    fewer fields, never crash.
    """

    label = instance._meta.label
    fields = AUDITABLE_FIELDS.get(label, ())
    snapshot: dict[str, Any] = {}
    for field_name in fields:
        try:
            snapshot[field_name] = getattr(instance, field_name)
        except AttributeError:  # pragma: no cover — defensive
            continue
    return snapshot


def _diff_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Build the ``{field: {before, after}}`` shape the service stores.

    Only fields whose values differ are included. That keeps
    no-op saves (like a ``save(update_fields=[]``) invisible.
    """

    changes: dict[str, dict[str, Any]] = {}
    for field in set(before) | set(after):
        old = before.get(field)
        new = after.get(field)
        if old != new:
            changes[field] = {"before": old, "after": new}
    return changes


def _resolve_org(instance: Model) -> Model | None:
    """Return the instance's owning ``Organization`` if any.

    The service's ``org`` argument takes an Organization instance or
    ``None``. We read the ``org`` attribute off the instance when
    present. Any model whose audit trail should be scoped to an org
    but whose ``org`` attribute doesn't resolve lands as a global
    entry (``org=NULL``) — better than raising, which would break
    saves.
    """

    return getattr(instance, "org", None)


# ── signal receivers ────────────────────────────────────────────────


def _on_pre_save(sender: type[Model], instance: Model, **kwargs: Any) -> None:
    """Stash the pre-save field snapshot on the instance.

    For creates (``instance.pk is None``) there is nothing to
    snapshot — the post-save receiver treats missing snapshots as
    "this was a create" and records the create action instead of a
    diff.
    """

    if not model_audit_registry.is_audited(sender._meta.label):
        return
    if instance.pk is None:
        return

    try:
        old = type(instance).objects.get(pk=instance.pk)
    except type(instance).DoesNotExist:  # pragma: no cover — rare race
        return

    setattr(instance, _PRE_SAVE_SLOT, _snapshot_auditable_fields(old))


def _on_post_save(
    sender: type[Model],
    instance: Model,
    created: bool,  # noqa: FBT001  Django signal signature — positional bool is required.
    **kwargs: Any,
) -> None:
    """Record a create or update audit entry based on the diff.

    Updates that touched only non-whitelisted fields produce no entry
    — we only want the audit log to carry events whose payload has
    forensic value.
    """

    triplet = model_audit_registry.actions_for(sender._meta.label)
    if triplet is None:
        return

    context = get_current_context()
    common_kwargs = {
        "actor": context.actor,
        "org": _resolve_org(instance),
        "target": instance,
        "request_id": context.request_id,
    }

    if created:
        if triplet.create is None:
            return
        AuditLogService.record(
            action=triplet.create,
            changes=None,
            **common_kwargs,
        )
        return

    if triplet.update is None:
        return

    before = getattr(instance, _PRE_SAVE_SLOT, None)
    after = _snapshot_auditable_fields(instance)
    # Drop the snapshot slot regardless of whether we record — the
    # instance might be reused (rare but possible) and a stale
    # snapshot could mask a subsequent change.
    if hasattr(instance, _PRE_SAVE_SLOT):
        delattr(instance, _PRE_SAVE_SLOT)

    if before is None:
        # No pre-save snapshot (e.g. the model was loaded via
        # ``save(force_insert=False)`` on a fresh instance, or
        # pre_save was skipped). Record the update with no diff so
        # the operator still sees the event.
        AuditLogService.record(
            action=triplet.update,
            changes=None,
            **common_kwargs,
        )
        return

    changes = _diff_snapshots(before, after)
    if not changes:
        # Nothing audit-relevant changed — skip to keep the log quiet.
        return

    AuditLogService.record(
        action=triplet.update,
        changes=changes,
        **common_kwargs,
    )


def _on_pre_delete(sender: type[Model], instance: Model, **kwargs: Any) -> None:
    """Record the deletion *before* the row is gone.

    Captured on ``pre_delete`` rather than ``post_delete`` so the
    instance still has a PK and its auditable field values. The
    service snapshots those into ``target_repr``; the ``changes``
    blob is the whitelisted "final state" of the row.
    """

    triplet = model_audit_registry.actions_for(sender._meta.label)
    if triplet is None or triplet.delete is None:
        return

    context = get_current_context()
    final_state = _snapshot_auditable_fields(instance)
    AuditLogService.record(
        action=triplet.delete,
        actor=context.actor,
        org=_resolve_org(instance),
        target=instance,
        request_id=context.request_id,
        # On delete the "before" values are the ``final_state``; the
        # ``after`` column is None because the row no longer exists.
        changes={
            field: {"before": value, "after": None}
            for field, value in final_state.items()
        }
        or None,
    )


# ── wiring ──────────────────────────────────────────────────────────


def connect_model_audit_receivers() -> None:
    """Connect the three generic signal handlers.

    Registering *listeners*, not filtering by sender here — the
    handlers themselves check the registry to decide whether to act.
    That way a model can opt in via ``register_model_audit()`` without
    also needing to touch the signal wiring.
    """

    pre_save.connect(
        _on_pre_save,
        dispatch_uid="validibot_audit.model_audit.pre_save",
    )
    post_save.connect(
        _on_post_save,
        dispatch_uid="validibot_audit.model_audit.post_save",
    )
    pre_delete.connect(
        _on_pre_delete,
        dispatch_uid="validibot_audit.model_audit.pre_delete",
    )


def register_builtin_model_audits() -> None:
    """Register Workflow / Ruleset / Membership for audit capture.

    Split from the plain connect() so tests can exercise the registry
    without committing to the real-world registrations.
    """

    # Local imports keep this module importable at app-config time
    # without forcing the order of app loading.
    from validibot.users.models import Membership
    from validibot.validations.models import Ruleset
    from validibot.workflows.models import Workflow

    model_audit_registry.register(
        Workflow,
        create=AuditAction.WORKFLOW_CREATED,
        update=AuditAction.WORKFLOW_UPDATED,
        delete=AuditAction.WORKFLOW_DELETED,
    )
    model_audit_registry.register(
        Ruleset,
        create=AuditAction.RULESET_CREATED,
        update=AuditAction.RULESET_UPDATED,
        delete=AuditAction.RULESET_DELETED,
    )
    # Memberships: only update events are audited here — join/leave
    # will be captured by dedicated invitation and removal hooks in a
    # later session. ``is_active`` changes produce a
    # MEMBER_ROLE_CHANGED entry today because that's the closest
    # existing action code; a dedicated MEMBER_SUSPENDED / REINSTATED
    # split can come later without touching the capture layer.
    model_audit_registry.register(
        Membership,
        update=AuditAction.MEMBER_ROLE_CHANGED,
    )
