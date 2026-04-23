"""Read-only Django admin for the audit tables.

Audit data is append-only — the whole point of the two-layer actor /
entry split is that rows are never mutated in ordinary operation.
The admin here reflects that:

* Neither model can be **added**, **changed**, or **deleted** through
  the admin UI. Attempts return "you don't have permission" per the
  overridden ``has_*_permission`` methods.
* ``AuditLogEntry`` is browseable and filterable so staff can
  investigate entries for incidents without going through the Pro UI
  (which is org-scoped and may hide cross-org context).
* Fields are all ``readonly`` so even a superuser cannot accidentally
  mutate an entry.

Deletion of audit entries is genuinely hard — the rows have ``PROTECT``
FKs from ``AuditLogEntry.actor``, and the Phase-3 erasure workflow
``sanitises`` PII in place rather than deleting anything. If staff
ever need to delete entries (e.g. because a broken test migration
created test data in production), that's a database-level manual
procedure with explicit audit accounting (``AUDIT_ENTRY_SANITISED``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib import admin
from django.utils.html import format_html

from validibot.audit.models import AuditActor
from validibot.audit.models import AuditLogEntry

if TYPE_CHECKING:
    from django.utils.safestring import SafeString


class _ReadOnlyAdmin(admin.ModelAdmin):
    """Disable all mutation routes for an admin model.

    Audit rows are immutable post-creation; surfacing add/change/
    delete buttons in the admin would be both misleading and
    dangerous. Subclasses can still define ``list_display`` etc. —
    this mixin only constrains *mutation*, not presentation.
    """

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        # ``has_change_permission`` is also what controls access to
        # the detail view. We return True so staff can *view* the
        # entry (list / drill-in) and rely on the readonly fields
        # to prevent edits.
        return request.user.is_active and request.user.is_staff

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(AuditActor)
class AuditActorAdmin(_ReadOnlyAdmin):
    """Identity-layer admin: searchable by user / email, not editable."""

    list_display = (
        "pk",
        "user",
        "email_display",
        "ip_address",
        "erased_at",
        "created_at",
    )
    list_filter = ("erased_at",)
    search_fields = ("user__email", "user__username", "email", "ip_address")
    ordering = ("-created_at",)
    readonly_fields = (
        "user",
        "email",
        "ip_address",
        "user_agent",
        "erased_at",
        "created_at",
    )

    @admin.display(description="Email", ordering="email")
    def email_display(self, obj: AuditActor) -> SafeString:
        """Visual cue for erased actors.

        Erased actors carry ``email=None`` and an ``erased_at``
        timestamp. Rendering that as empty text in the list view
        makes it ambiguous (did the actor never have an email?). We
        render "(erased)" explicitly so staff can tell the difference
        at a glance.
        """

        if obj.erased_at is not None:
            return format_html('<span class="text-muted">(erased)</span>')
        return format_html("{}", obj.email or "")


@admin.register(AuditLogEntry)
class AuditLogEntryAdmin(_ReadOnlyAdmin):
    """Event-layer admin: full drill-in, filterable, read-only."""

    list_display = (
        "pk",
        "occurred_at",
        "action",
        "org",
        "actor",
        "target_type",
        "target_repr",
    )
    list_filter = ("action", "target_type")
    search_fields = (
        "actor__user__email",
        "actor__email",
        "target_id",
        "target_repr",
        "request_id",
    )
    ordering = ("-occurred_at",)
    readonly_fields = (
        "actor",
        "org",
        "action",
        "target_type",
        "target_id",
        "target_repr",
        "changes",
        "metadata",
        "request_id",
        "occurred_at",
    )

    # Prefetch to avoid N+1 on the list page. The (org, -occurred_at)
    # index already makes the queryset fast; these ``select_related``
    # calls save a round-trip per row on column rendering.
    list_select_related = ("actor", "actor__user", "org")
