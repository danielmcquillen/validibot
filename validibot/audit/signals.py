"""Signal receivers that translate auth + model events into audit entries.

The receivers are the initial capture-points slice:

* **Login succeeded** — Django's ``user_logged_in`` → ``LOGIN_SUCCEEDED``.
* **Login failed** — Django's ``user_login_failed`` → ``LOGIN_FAILED``.
  Fired for both "username unknown" and "password wrong"; the signal
  payload doesn't distinguish, so neither do we.
* **Password changed** — allauth's ``password_changed`` →
  ``PASSWORD_CHANGED``.
* **API token created / revoked** — post-save/post-delete on
  ``rest_framework.authtoken.Token`` → ``API_KEY_CREATED`` /
  ``API_KEY_REVOKED``. That's Validibot's "API key" model.

Each receiver pulls actor/request-id from
``validibot.audit.context.get_current_context`` so the values come
from the request middleware rather than being rethreaded through the
signal ``kwargs``. Auth signals arrive with a ``request`` instance in
``kwargs`` but not every capture point has one — the token
post-save, for example, can fire from a Celery worker with no request
context at all, and the service handles that cleanly by writing an
entry with ``actor=None``.

Wiring happens in ``validibot.audit.apps.AuditConfig.ready()`` — we
avoid decorator-based ``@receiver`` auto-registration because it makes
signal wiring invisible from the app's ``AppConfig`` and hard to
disable in tests.
"""

from __future__ import annotations

import logging
from typing import Any

from allauth.account.signals import email_added
from allauth.account.signals import email_changed
from allauth.account.signals import email_confirmed
from allauth.account.signals import email_removed
from allauth.account.signals import password_changed
from allauth.account.signals import password_reset
from allauth.account.signals import user_logged_out
from allauth.mfa.signals import authentication_failed
from allauth.mfa.signals import authenticator_added
from allauth.mfa.signals import authenticator_removed
from django.contrib.auth.signals import user_logged_in
from django.contrib.auth.signals import user_login_failed
from django.db.models.signals import post_delete
from django.db.models.signals import post_save
from rest_framework.authtoken.models import Token

from validibot.audit.constants import AuditAction
from validibot.audit.context import get_current_context
from validibot.audit.services import ActorSpec
from validibot.audit.services import AuditLogService

logger = logging.getLogger(__name__)


def _actor_for_signal(user: Any, request: Any) -> ActorSpec:
    """Build the actor for a signal fired inside a request.

    Prefers the signal's own ``user`` argument when present (more
    specific than the middleware's resolved user — e.g. for login
    signals the middleware has ``AnonymousUser`` but the signal has
    the user who just logged in). Falls back to the middleware's
    ``ActorSpec`` for IP / user-agent context.
    """

    context_actor = get_current_context().actor

    # Don't overwrite a real user with None — but DO override when the
    # signal carries a better identity than the middleware snapshot.
    resolved_user = user if user is not None else context_actor.user

    # Anonymous allauth users can sneak through as an AnonymousUser
    # instance; treat that as "no user" for the actor row.
    if resolved_user is not None and not getattr(
        resolved_user,
        "is_authenticated",
        True,
    ):
        resolved_user = None

    return ActorSpec(
        user=resolved_user,
        email=getattr(resolved_user, "email", None) if resolved_user else None,
        ip_address=context_actor.ip_address
        or (_ip_from_request(request) if request is not None else None),
        user_agent=context_actor.user_agent
        or (request.headers.get("user-agent", "") if request is not None else ""),
    )


def _ip_from_request(request: Any) -> str | None:
    """Mirror the middleware's IP extraction for signals fired without it.

    Duplicated in miniature rather than imported from the middleware
    class because (a) this helper runs in signal-handler context where
    the middleware hasn't necessarily seen the request yet, and (b)
    keeps the signal module free of middleware imports.
    """

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


# ── allauth / Django auth ────────────────────────────────────────────


def on_user_logged_in(sender, request, user, **kwargs) -> None:
    """Record a ``LOGIN_SUCCEEDED`` entry for a successful login.

    Django fires this from ``login()`` so the signal is reliable for
    both session-based web logins and API logins that go through
    ``login()``. API-token auth does NOT fire it — that's audited by
    a separate DRF-auth hook (out of scope for Phase 1).
    """

    context = get_current_context()
    AuditLogService.record(
        action=AuditAction.LOGIN_SUCCEEDED,
        actor=_actor_for_signal(user, request),
        request_id=context.request_id,
        metadata=_login_metadata(request),
    )


def on_user_login_failed(sender, credentials, request=None, **kwargs) -> None:
    """Record a ``LOGIN_FAILED`` entry for a failed authentication.

    ``credentials`` may contain the attempted username — Django
    scrubs the password before passing the dict in, but anything else
    (including potentially-sensitive usernames) is still present.
    We record only the username, not the full ``credentials`` dict.
    Entries have ``actor.user=None`` because the authentication never
    produced a valid user.
    """

    context = get_current_context()
    attempted_username = _safe_username_from_credentials(credentials)

    AuditLogService.record(
        action=AuditAction.LOGIN_FAILED,
        actor=ActorSpec(
            user=None,
            email=attempted_username,
            ip_address=context.actor.ip_address
            or (_ip_from_request(request) if request is not None else None),
            user_agent=context.actor.user_agent
            or (request.headers.get("user-agent", "") if request is not None else ""),
        ),
        request_id=context.request_id,
        metadata={
            "attempted_username": attempted_username or "",
        },
    )


def on_password_changed(sender, request, user, **kwargs) -> None:
    """Record a ``PASSWORD_CHANGED`` entry.

    allauth fires this after a successful password change regardless
    of whether the user initiated it interactively or an admin forced
    it; both are audit-relevant.
    """

    context = get_current_context()
    AuditLogService.record(
        action=AuditAction.PASSWORD_CHANGED,
        actor=_actor_for_signal(user, request),
        request_id=context.request_id,
        target=user,
    )


# ── session / MFA / email identity events ───────────────────────────


def on_user_logged_out(sender, request, user, **kwargs) -> None:
    """Record a ``SESSION_REVOKED`` entry when a user logs out.

    The inverse of ``on_user_logged_in`` — a sign-out should be as
    visible in the audit trail as a sign-in. ``user`` may be ``None``
    if the session was already anonymous; the service tolerates a
    user-less actor.
    """

    context = get_current_context()
    AuditLogService.record(
        action=AuditAction.SESSION_REVOKED,
        actor=_actor_for_signal(user, request),
        request_id=context.request_id,
        target=user,
        metadata=_login_metadata(request),
    )


def on_password_reset(sender, request, user, **kwargs) -> None:
    """Record ``PASSWORD_RESET_REQUESTED`` for a reset via the email link.

    allauth fires ``password_reset`` once the reset *completes*. We file
    it under the closest existing action and tag ``metadata.phase`` so a
    reader can tell it apart from an interactive ``PASSWORD_CHANGED``.
    """

    context = get_current_context()
    AuditLogService.record(
        action=AuditAction.PASSWORD_RESET_REQUESTED,
        actor=_actor_for_signal(user, request),
        request_id=context.request_id,
        target=user,
        metadata={"phase": "completed"},
    )


def on_mfa_authenticator_added(
    sender,
    request,
    user,
    authenticator,
    **kwargs,
) -> None:
    """Record ``MFA_ENABLED`` when an authenticator is added.

    Captures only the factor *type* (totp / recovery_codes / webauthn)
    for incident triage — never the secret material on the authenticator.
    """

    context = get_current_context()
    AuditLogService.record(
        action=AuditAction.MFA_ENABLED,
        actor=_actor_for_signal(user, request),
        request_id=context.request_id,
        target=user,
        metadata=_mfa_metadata(authenticator),
    )


def on_mfa_authenticator_removed(
    sender,
    request,
    user,
    authenticator,
    **kwargs,
) -> None:
    """Record ``MFA_DISABLED`` when an authenticator is removed."""

    context = get_current_context()
    AuditLogService.record(
        action=AuditAction.MFA_DISABLED,
        actor=_actor_for_signal(user, request),
        request_id=context.request_id,
        target=user,
        metadata=_mfa_metadata(authenticator),
    )


def on_mfa_authentication_failed(sender, request, user, **kwargs) -> None:
    """Record ``MFA_CHALLENGE_FAILED`` on a failed second-factor check.

    A burst of these is a brute-force / phishing signal, so it gets its
    own action for log-based alerting.
    """

    context = get_current_context()
    AuditLogService.record(
        action=AuditAction.MFA_CHALLENGE_FAILED,
        actor=_actor_for_signal(user, request),
        request_id=context.request_id,
        target=user,
        metadata=_mfa_metadata(kwargs.get("authenticator")),
    )


def on_email_added(sender, request, user, email_address, **kwargs) -> None:
    """Record ``EMAIL_ADDED`` — the *fact* only, never the address value."""

    _record_email_event(AuditAction.EMAIL_ADDED, user, request)


def on_email_changed(
    sender,
    request,
    user,
    from_email_address,
    to_email_address,
    **kwargs,
) -> None:
    """Record ``EMAIL_CHANGED`` — the fact only, not the before/after values.

    The changed addresses are PII; recording that a change happened (and
    by whom, when, from where) is what matters for account-takeover
    forensics, per the audit design doc's email special-case.
    """

    _record_email_event(AuditAction.EMAIL_CHANGED, user, request)


def on_email_confirmed(sender, request, email_address, **kwargs) -> None:
    """Record ``EMAIL_VERIFIED``.

    Unlike the other email signals, ``email_confirmed`` carries no
    ``user`` argument — we resolve the owner from ``email_address.user``.
    """

    user = getattr(email_address, "user", None)
    _record_email_event(AuditAction.EMAIL_VERIFIED, user, request)


def on_email_removed(sender, request, user, email_address, **kwargs) -> None:
    """Record ``EMAIL_REMOVED`` — the fact only."""

    _record_email_event(AuditAction.EMAIL_REMOVED, user, request)


# ── DRF auth tokens (Validibot "API keys") ──────────────────────────


def on_token_created_or_updated(sender, instance, created, **kwargs) -> None:
    """Record ``API_KEY_CREATED`` when a DRF ``Token`` row is first saved.

    Updates on an existing Token row don't produce an entry — a DRF
    Token only stores ``(user, key, created)`` and neither is meant to
    change after creation. If someone does manage to update one, that
    lands in the admin-action bridge via Django's ``admin.LogEntry``.

    Model signals don't carry a request object, so actor attribution
    comes purely from the middleware context. That's correct: a token
    created inside a Celery task has no authenticated user, while a
    token created inside a request flow will have the right user
    courtesy of ``AuditContextMiddleware``.
    """

    if not created:
        return

    context = get_current_context()
    AuditLogService.record(
        action=AuditAction.API_KEY_CREATED,
        actor=_actor_for_signal(instance.user, request=None),
        target_type="authtoken.Token",
        target_id=str(instance.pk) if instance.pk is not None else "",
        target_repr=f"Token for {instance.user}",
        request_id=context.request_id,
    )


def on_token_deleted(sender, instance, **kwargs) -> None:
    """Record ``API_KEY_REVOKED`` when a DRF ``Token`` row is deleted."""

    context = get_current_context()
    AuditLogService.record(
        action=AuditAction.API_KEY_REVOKED,
        actor=_actor_for_signal(instance.user, request=None),
        target_type="authtoken.Token",
        target_id=str(instance.pk) if instance.pk is not None else "",
        target_repr=f"Token for {instance.user}",
        request_id=context.request_id,
    )


# ── membership / guest lifecycle ─────────────────────────────────────


def on_member_invite_created(sender, instance, created, **kwargs) -> None:
    """Record ``MEMBER_INVITED`` when a ``MemberInvite`` row is created.

    The invitee's email is third-party PII and is deliberately *not*
    captured. We record the inviting org, the proposed roles and the
    status with a PII-free ``target_repr`` — enough to answer "who was
    invited to what, by whom" without writing an unrelated party's
    address into the immutable layer.
    """

    if not created:
        return

    context = get_current_context()
    AuditLogService.record(
        action=AuditAction.MEMBER_INVITED,
        actor=context.actor,
        org=getattr(instance, "org", None),
        target_type="users.MemberInvite",
        target_id=str(instance.pk),
        target_repr=f"Member invite #{instance.pk}",
        metadata={
            "roles": list(instance.roles) if instance.roles else [],
            "status": instance.status,
        },
        request_id=context.request_id,
    )


def on_org_guest_access_created(sender, instance, created, **kwargs) -> None:
    """Record ``GUEST_GRANTED`` when org-wide guest access is created.

    Covers the org-wide ALL-scope grant (``OrgGuestAccess``). Per-workflow
    grants and the bulk-``update()`` revocations live in the sharing /
    members views and are audited there — a QuerySet ``.update()`` does
    not fire ``post_save``. The guest's *id* is recorded, never an email.
    """

    if not created:
        return

    context = get_current_context()
    AuditLogService.record(
        action=AuditAction.GUEST_GRANTED,
        actor=context.actor,
        org=getattr(instance, "org", None),
        target_type="workflows.OrgGuestAccess",
        target_id=str(instance.pk),
        target_repr=f"Org guest access #{instance.pk}",
        metadata={"guest_user_id": getattr(instance, "user_id", None)},
        request_id=context.request_id,
    )


# ── wiring ──────────────────────────────────────────────────────────


def connect_signal_receivers() -> None:
    """Attach every Phase-1 receiver. Called from ``AuditConfig.ready()``.

    ``dispatch_uid`` values guard against double-connection when the
    app registry is reloaded mid-test or when another app imports the
    module by accident. Without the uids, each reconnect would produce
    duplicate audit entries per event.
    """

    user_logged_in.connect(
        on_user_logged_in,
        dispatch_uid="validibot_audit.on_user_logged_in",
    )
    user_login_failed.connect(
        on_user_login_failed,
        dispatch_uid="validibot_audit.on_user_login_failed",
    )
    password_changed.connect(
        on_password_changed,
        dispatch_uid="validibot_audit.on_password_changed",
    )
    user_logged_out.connect(
        on_user_logged_out,
        dispatch_uid="validibot_audit.on_user_logged_out",
    )
    password_reset.connect(
        on_password_reset,
        dispatch_uid="validibot_audit.on_password_reset",
    )
    authenticator_added.connect(
        on_mfa_authenticator_added,
        dispatch_uid="validibot_audit.on_mfa_authenticator_added",
    )
    authenticator_removed.connect(
        on_mfa_authenticator_removed,
        dispatch_uid="validibot_audit.on_mfa_authenticator_removed",
    )
    authentication_failed.connect(
        on_mfa_authentication_failed,
        dispatch_uid="validibot_audit.on_mfa_authentication_failed",
    )
    email_added.connect(
        on_email_added,
        dispatch_uid="validibot_audit.on_email_added",
    )
    email_changed.connect(
        on_email_changed,
        dispatch_uid="validibot_audit.on_email_changed",
    )
    email_confirmed.connect(
        on_email_confirmed,
        dispatch_uid="validibot_audit.on_email_confirmed",
    )
    email_removed.connect(
        on_email_removed,
        dispatch_uid="validibot_audit.on_email_removed",
    )
    post_save.connect(
        on_token_created_or_updated,
        sender=Token,
        dispatch_uid="validibot_audit.on_token_created_or_updated",
    )
    post_delete.connect(
        on_token_deleted,
        sender=Token,
        dispatch_uid="validibot_audit.on_token_deleted",
    )

    # Membership / guest lifecycle. Local imports keep the audit app from
    # importing other apps' models at module-load time.
    from validibot.users.models import MemberInvite
    from validibot.workflows.models import OrgGuestAccess

    post_save.connect(
        on_member_invite_created,
        sender=MemberInvite,
        dispatch_uid="validibot_audit.on_member_invite_created",
    )
    post_save.connect(
        on_org_guest_access_created,
        sender=OrgGuestAccess,
        dispatch_uid="validibot_audit.on_org_guest_access_created",
    )


# ── local helpers ───────────────────────────────────────────────────


def _safe_username_from_credentials(credentials: Any) -> str | None:
    """Pull the attempted username out of a ``user_login_failed`` payload.

    Django passes a dict; allauth sometimes renames ``username`` to
    ``login`` or ``email``. We try the common keys in order and stop
    at the first match so the audit entry surfaces whatever the
    authenticator had available.
    """

    if not isinstance(credentials, dict):
        return None
    for key in ("username", "email", "login"):
        value = credentials.get(key)
        if value:
            return str(value)
    return None


def _login_metadata(request: Any) -> dict[str, str] | None:
    """Build per-login metadata: path + channel.

    Mirrors the information ``tracking/signals.py`` already captures
    so the analytics and audit streams agree on how to label a login.
    Only written when a request object is available (session auth).
    """

    if request is None:
        return None
    path = getattr(request, "path", "") or ""
    return {
        "path": path,
        "channel": "api" if path.startswith("/api/") else "web",
    }


def _mfa_metadata(authenticator: Any) -> dict[str, str] | None:
    """Return ``{"authenticator_type": ...}`` for an MFA event.

    Only the *kind* of factor (totp / recovery_codes / webauthn) is
    recorded — the secret / credential material on the authenticator is
    never read.
    """

    auth_type = getattr(authenticator, "type", None)
    return {"authenticator_type": str(auth_type)} if auth_type else None


def _record_email_event(action: AuditAction, user: Any, request: Any) -> None:
    """Write an email-lifecycle entry that records the fact, not the value.

    ``target_repr`` is forced to a PII-free ``User #<pk>`` label rather
    than letting the service derive it from ``str(user)`` (which could be
    an email-shaped username). The address itself is never passed in, so
    there is nowhere on the entry for it to land.
    """

    context = get_current_context()
    user_pk = getattr(user, "pk", None)
    AuditLogService.record(
        action=action,
        actor=_actor_for_signal(user, request),
        request_id=context.request_id,
        target_type="users.User",
        target_id=str(user_pk) if user_pk is not None else "",
        target_repr=f"User #{user_pk}" if user_pk is not None else "",
    )
