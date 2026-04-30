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

from allauth.account.signals import password_changed
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
