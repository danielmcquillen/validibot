"""Install the per-request audit context for every HTTP request.

The middleware is thin on purpose — all it does is:

1. Resolve the actor from ``request.user`` (anonymous = no actor user).
2. Extract the client IP, preferring ``X-Forwarded-For`` because
   Cloud Run and most reverse proxies terminate TLS upstream.
3. Mint a request id so audit entries can be cross-referenced against
   Cloud Logging markers and application logs.
4. Stash the whole bundle on the ``contextvars`` slot in
   ``validibot.audit.context``, and reset it on the way out via a
   ``try/finally`` so an exception inside the view doesn't leave the
   context set on the task.

Where it sits in ``MIDDLEWARE``: after ``AuthenticationMiddleware`` so
``request.user`` is already resolved, but before any business-logic
middleware that might want to emit audit entries itself. See
``config/settings/base.py`` for the final ordering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from validibot.audit.context import AuditRequestContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest
    from django.http import HttpResponse
from validibot.audit.context import new_request_id
from validibot.audit.context import reset_current_context
from validibot.audit.context import set_current_context
from validibot.audit.services import ActorSpec


class AuditContextMiddleware:
    """Per-request context manager for the audit capture layer.

    Django middleware contract: call the wrapped ``get_response`` with
    the request and return its response. We bracket that call with
    context set/reset so every signal handler invoked during view
    execution can resolve the actor, IP, and request id without
    threading them through as arguments.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self._get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Install the audit context for the duration of this request.

        The ``try/finally`` guarantees the reset runs even when a view
        raises — without it, a later request dispatched on the same
        async task would inherit a stale actor and produce mis-attributed
        audit entries.
        """

        context = self._build_context(request)
        token = set_current_context(context)
        # Attach the request id to the request object too so downstream
        # code (e.g. structured logging middleware) can read it without
        # touching the audit module.
        request.audit_request_id = context.request_id
        try:
            return self._get_response(request)
        finally:
            reset_current_context(token)

    # ── helpers ─────────────────────────────────────────────────────

    @classmethod
    def _build_context(cls, request: HttpRequest) -> AuditRequestContext:
        """Assemble the ``AuditRequestContext`` for a single request."""

        user = getattr(request, "user", None)
        # Anonymous users get no ``user`` attribution. ``AnonymousUser``
        # has ``is_authenticated=False`` — map that to ``None`` so the
        # actor row isn't linked to a pseudo-user object.
        if user is None or not getattr(user, "is_authenticated", False):
            user = None

        actor = ActorSpec(
            user=user,
            ip_address=cls._extract_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        )
        return AuditRequestContext(
            actor=actor,
            request_id=new_request_id(),
        )

    @staticmethod
    def _extract_client_ip(request: HttpRequest) -> str | None:
        """Return the caller's IP, preferring ``X-Forwarded-For``.

        On Cloud Run and behind a Cloud Load Balancer the direct peer
        is the load balancer, so ``REMOTE_ADDR`` points at Google
        infrastructure rather than the real client. The balancer
        injects the original client IP as the leftmost entry of
        ``X-Forwarded-For`` — that's the value we want in audit
        entries.

        Self-hosted deployments behind nginx/Traefik follow the same
        convention. Deployments that expose Django directly to the
        internet (rare; don't do it) fall back to ``REMOTE_ADDR``.

        **Spoofing note:** this trusts ``X-Forwarded-For`` blindly.
        That is correct when a trusted reverse proxy terminates client
        connections; it is INSECURE if Django receives requests
        directly. We document the assumption rather than encode a
        trusted-proxy allowlist because the actual trust boundary
        varies per deployment and would need its own setting.
        """

        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # ``X-Forwarded-For: client, proxy1, proxy2`` — leftmost is
            # the original client.
            return forwarded.split(",")[0].strip() or None

        return request.META.get("REMOTE_ADDR") or None
