"""Audit action codes and per-model field whitelists.

The ``AuditAction`` enum lists every kind of event the audit log
recognises. Each action maps onto a category with a retention policy
(see ``retention_for_action()`` below and
``validibot-project/docs/observability/logging-taxonomy.md``).

``AUDITABLE_FIELDS`` declares which fields the audit log is allowed to
snapshot into the ``changes`` JSON blob. Anything not in the whitelist
is recorded as ``{"<field>": "<redacted>"}`` so operators see the *fact*
of a change without leaking secrets. ADR-2026-04-16 §4 (field-level
data sanitisation) spells out the rules.
"""

from __future__ import annotations

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _


class AuditAction(TextChoices):
    """Machine-readable audit action identifier.

    Start narrow. Every action has a per-capture-path hook and a
    retention commitment; adding one is not free. The three categories
    here match the ADR's Phase 1 scope.
    """

    # ── Configuration changes (table stakes) ───────────────────────
    WORKFLOW_CREATED = "workflow_created", _("Workflow Created")
    WORKFLOW_UPDATED = "workflow_updated", _("Workflow Updated")
    WORKFLOW_DELETED = "workflow_deleted", _("Workflow Deleted")
    RULESET_CREATED = "ruleset_created", _("Ruleset Created")
    RULESET_UPDATED = "ruleset_updated", _("Ruleset Updated")
    RULESET_DELETED = "ruleset_deleted", _("Ruleset Deleted")
    VALIDATOR_ADDED = "validator_added", _("Validator Added")
    VALIDATOR_UPDATED = "validator_updated", _("Validator Updated")
    VALIDATOR_REMOVED = "validator_removed", _("Validator Removed")
    MEMBER_INVITED = "member_invited", _("Member Invited")
    MEMBER_ROLE_CHANGED = "member_role_changed", _("Member Role Changed")
    MEMBER_REMOVED = "member_removed", _("Member Removed")
    GUEST_GRANTED = "guest_granted", _("Guest Access Granted")
    GUEST_REVOKED = "guest_revoked", _("Guest Access Revoked")
    API_KEY_CREATED = "api_key_created", _("API Key Created")
    API_KEY_REVOKED = "api_key_revoked", _("API Key Revoked")

    # ── Security events (incident response + compliance) ───────────
    LOGIN_SUCCEEDED = "login_succeeded", _("Login Succeeded")
    LOGIN_FAILED = "login_failed", _("Login Failed")
    MFA_ENABLED = "mfa_enabled", _("MFA Enabled")
    MFA_DISABLED = "mfa_disabled", _("MFA Disabled")
    MFA_CHALLENGE_FAILED = "mfa_challenge_failed", _("MFA Challenge Failed")
    PASSWORD_CHANGED = "password_changed", _("Password Changed")
    PASSWORD_RESET_REQUESTED = (
        "password_reset_requested",
        _("Password Reset Requested"),
    )
    SESSION_REVOKED = "session_revoked", _("Session Revoked")

    # ── Admin actions (insider-threat investigation) ───────────────
    ADMIN_OBJECT_CHANGED = "admin_object_changed", _("Admin Object Changed")

    # ── Privacy operations (auditing the erasure workflow itself) ──
    USER_ERASURE_REQUESTED = (
        "user_erasure_requested",
        _("User Erasure Requested"),
    )
    USER_ERASURE_COMPLETED = (
        "user_erasure_completed",
        _("User Erasure Completed"),
    )
    AUDIT_ENTRY_SANITISED = (
        "audit_entry_sanitised",
        _("Audit Entry Sanitised"),
    )


# ── Field whitelists per model ─────────────────────────────────────
# Only these fields are allowed into the ``changes`` snapshot. Anything
# else becomes ``{"<field>": "<redacted>"}`` — we record the *fact* of a
# change without its value. The dict key is the Django model's
# ``Meta.label`` (``app_label.ModelName``).

AUDITABLE_FIELDS: dict[str, tuple[str, ...]] = {
    # Workflow — name/description/publication flags. Never stores the
    # validator config itself (may contain customer secrets).
    "workflows.Workflow": (
        "name",
        "description",
        "is_public",
        "agent_access_enabled",
        "agent_public_discovery",
    ),
    # Ruleset — name only. The assertion bodies may reference customer
    # field paths, so the full diff goes behind the redaction line.
    # Lives in the ``validations`` app, not ``workflows``.
    "validations.Ruleset": ("name",),
    # Membership — ``roles`` is an M2M through ``MembershipRole``, so
    # direct role changes fire on that through-table rather than on
    # ``Membership`` itself. Here we capture the suspension flag; role
    # additions/removals will get a dedicated hook on the through-table
    # alongside MEMBER_ROLE_CHANGED in a future session.
    "users.Membership": ("is_active",),
    # DRF auth Token — what Validibot uses as its "API key" surface.
    # The token's ``key`` field IS the credential, so it must NEVER
    # appear in ``changes``. No fields are whitelisted; the audit
    # entry captures the fact of create/revoke via dedicated
    # API_KEY_CREATED / API_KEY_REVOKED actions and only records
    # metadata like the associated user (via ``target_repr``).
    "authtoken.Token": (),
    # User — administrative status toggles only. Email / name changes
    # are recorded as the *fact* of a change, never the value (GDPR
    # considerations).
    "users.User": ("is_active", "is_staff", "is_superuser"),
}


# ── Retention tiers ────────────────────────────────────────────────
# The numeric values are Phase-2 Cloud Run Job parameters — they don't
# affect Phase-1 behaviour. Kept here so the archival worker and the
# privacy-policy documentation read from one source of truth.

RETENTION_HOT_DAYS_DEFAULT = 90

RETENTION_COLD_DAYS: dict[AuditAction, int] = {
    # Login success/failure: 1 year cold
    AuditAction.LOGIN_SUCCEEDED: 365,
    AuditAction.LOGIN_FAILED: 365,
    AuditAction.MFA_CHALLENGE_FAILED: 365,
    # Config changes: 2 years (dispute window)
    AuditAction.WORKFLOW_CREATED: 365 * 2,
    AuditAction.WORKFLOW_UPDATED: 365 * 2,
    AuditAction.WORKFLOW_DELETED: 365 * 2,
    AuditAction.RULESET_CREATED: 365 * 2,
    AuditAction.RULESET_UPDATED: 365 * 2,
    AuditAction.RULESET_DELETED: 365 * 2,
    AuditAction.VALIDATOR_ADDED: 365 * 2,
    AuditAction.VALIDATOR_UPDATED: 365 * 2,
    AuditAction.VALIDATOR_REMOVED: 365 * 2,
    # Permission + member changes: 3 years (insider-threat window)
    AuditAction.MEMBER_INVITED: 365 * 3,
    AuditAction.MEMBER_ROLE_CHANGED: 365 * 3,
    AuditAction.MEMBER_REMOVED: 365 * 3,
    AuditAction.GUEST_GRANTED: 365 * 3,
    AuditAction.GUEST_REVOKED: 365 * 3,
    AuditAction.API_KEY_CREATED: 365 * 3,
    AuditAction.API_KEY_REVOKED: 365 * 3,
    # Admin actions: 3 years
    AuditAction.ADMIN_OBJECT_CHANGED: 365 * 3,
    # Privacy ops: indefinite — cold retention is -1, interpreted as
    # "never delete" by the archival job.
    AuditAction.USER_ERASURE_REQUESTED: -1,
    AuditAction.USER_ERASURE_COMPLETED: -1,
    AuditAction.AUDIT_ENTRY_SANITISED: -1,
}


def retention_cold_days_for(action: AuditAction) -> int:
    """Return the cold-storage retention window in days for an action.

    -1 means "never delete" (reserved for privacy-operation entries
    that document our own compliance with erasure requests).
    """

    return RETENTION_COLD_DAYS.get(action, 365 * 2)
