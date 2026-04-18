"""
Platform-agnostic authentication for worker-only API endpoints.

Worker endpoints — ``/api/v1/execute-validation-run/``, validator callbacks,
scheduled-task triggers — are invoked by infrastructure (task queues,
schedulers, validator sidecars), never by end users. What counts as
"trusted infrastructure" depends on ``DEPLOYMENT_TARGET``:

``TEST`` / ``LOCAL_DOCKER_COMPOSE`` / ``DOCKER_COMPOSE``
    Caller is the Django test client or a Celery worker.
    Verification: :class:`WorkerKeyAuthentication` (shared secret).

``GCP``
    Caller is Cloud Tasks, Cloud Scheduler, or a validator Cloud Run Job
    (via the GCE metadata server).
    Verification: :class:`CloudTasksOIDCAuthentication` (Google-signed
    OIDC identity token, strict audience + SA allowlist).

``AWS`` (placeholder; end-to-end support not yet implemented)
    Falls back to :class:`WorkerKeyAuthentication` until an AWS-native
    signature backend lands.

This module exposes :func:`get_worker_auth_classes`, a factory that returns
the authentication classes appropriate for the current deployment target.
:class:`~validibot.core.api.worker.WorkerOnlyAPIView` consumes the factory
in its ``get_authenticators()`` override so every worker endpoint picks up
the right authentication automatically — no per-view wiring required.

Why this matters (the original gap)
-----------------------------------
Before this module existed, ``WorkerOnlyAPIView`` relied on
:class:`WorkerKeyAuthentication` alone. On GCP that class is a no-op
because ``WORKER_API_KEY`` is intentionally unset, and we relied entirely
on Cloud Run IAM + Cloud Tasks OIDC tokens at the infrastructure layer.
Any IAM misconfiguration (``--allow-unauthenticated``, a shared ingress
rule, a domain mapping accident) would have turned the execute-validation
endpoint into a publicly-invocable RCE equivalent. Verifying the OIDC
token at the application layer closes that hole as defence in depth.

Prior art
---------
This mirrors :class:`validibot_cloud.agents.authentication.MCPServiceAuthentication`,
which already uses the same ``google.oauth2.id_token.verify_oauth2_token``
pattern with a reusable transport for MCP-to-Django service traffic. Keeping
the two implementations idiomatically identical makes the verification path
easy to audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import ClassVar

from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from validibot.core.api.worker_auth import WorkerKeyAuthentication
from validibot.core.constants import DeploymentTarget
from validibot.core.deployment import get_deployment_target

if TYPE_CHECKING:
    from rest_framework.request import Request

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerIdentity:
    """Structured identity information for an authenticated infrastructure caller.

    Returned as ``request.auth`` by worker-auth backends so that views,
    audit logs, and metrics can distinguish between caller types
    (Cloud Tasks service account, Celery worker, Cloud Scheduler, etc.)
    without re-parsing request headers.

    Attributes:
        source: Short identifier of the authentication path that succeeded
            (``"cloud_tasks_oidc"``, ``"worker_key"``, etc.). Useful for
            log lines and observability.
        email: Service-account email address when available (GCP OIDC
            path). Empty string for shared-secret paths where the caller
            has no OAuth identity.
    """

    source: str
    email: str = ""


# ---------------------------------------------------------------------------
# GCP: Cloud Tasks / Cloud Scheduler OIDC verification
# ---------------------------------------------------------------------------


class CloudTasksOIDCAuthentication(BaseAuthentication):
    """Verify inbound Google-signed OIDC identity tokens at the app layer.

    Cloud Tasks and Cloud Scheduler attach an OIDC identity token signed
    by a configured service account to every task/invocation. Cloud Run
    IAM verifies the token at the infrastructure edge; this class
    re-verifies it inside Django so that any IAM misconfiguration does
    not become a public-endpoint disaster.

    Checks performed:

    1. ``Authorization: Bearer <token>`` is present and parses.
    2. Token signature is valid against Google's OIDC discovery keys
       (``https://www.googleapis.com/oauth2/v3/certs``).
    3. Token ``aud`` claim equals ``TASK_OIDC_AUDIENCE``
       (default: ``WORKER_URL``).
    4. Token ``email_verified`` claim is ``True``.
    5. Token ``email`` claim is in ``TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS``
       (default: ``[CLOUD_TASKS_SERVICE_ACCOUNT]``).

    Checks (1)–(3) are handled by
    :func:`google.oauth2.id_token.verify_oauth2_token`; checks (4)–(5)
    are enforced here because the invoker-SA allowlist is our own
    application-level concern.

    The ``google-auth`` transport is lazy-initialised at the class level
    and reused across requests to avoid creating a new
    :class:`requests.Session` (and TCP connection pool) per verification.
    This matches the pattern in
    :class:`validibot_cloud.agents.authentication.MCPServiceAuthentication`.

    On success, returns ``(None, WorkerIdentity(...))`` — no Django user
    is associated because the caller is infrastructure, not a person.
    """

    # Lazy, reused across the whole process. ``None`` after init means
    # google-auth is not installed (shouldn't happen on a GCP deployment;
    # see docstring in :meth:`_get_google_transport`).
    _google_transport: ClassVar[object | None] = None
    _google_transport_initialised: ClassVar[bool] = False

    def authenticate_header(self, request: Request) -> str:
        """Advertise a Bearer challenge so auth failures become HTTP 401.

        DRF uses the first authenticator's ``authenticate_header()`` to
        populate the ``WWW-Authenticate`` header when returning 401.
        Cloud Tasks does not read this header, but surfacing it keeps
        the endpoint well-behaved for any generic OIDC client (and for
        ops staff running ``curl`` manually).
        """
        return 'Bearer realm="validibot-worker"'

    def authenticate(self, request: Request):
        """Verify the inbound OIDC identity token.

        Returns:
            ``(None, WorkerIdentity)`` on success. No Django user is
            attached because the caller is infrastructure.

        Raises:
            AuthenticationFailed: On any verification failure. We raise
                (rather than returning ``None`` to fall through) because
                on GCP there is no other authentication class to fall
                through to — a Bearer token is the only legitimate way
                to reach this endpoint.
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning(
                "Worker endpoint called without Bearer OIDC token "
                "(deployment=gcp, path=%s)",
                request.path,
            )
            raise AuthenticationFailed(
                "Missing OIDC identity token. Provide Authorization: Bearer <token>.",
            )

        token = auth_header[len("Bearer ") :]
        claims = self._verify_oidc_token(token)
        self._check_service_account_allowlist(claims)

        email = claims.get("email", "")
        return (None, WorkerIdentity(source="cloud_tasks_oidc", email=email))

    # -- Token verification --------------------------------------------------

    def _verify_oidc_token(self, token: str) -> dict:
        """Return verified claims or raise :class:`AuthenticationFailed`.

        Wraps :func:`google.oauth2.id_token.verify_oauth2_token`, which
        handles JWKS fetch/caching, signature verification, issuer check,
        audience check, and expiry check in one call.
        """
        transport = self._get_google_transport()
        if transport is None:
            # google-auth is a transitive dep of google-cloud-tasks,
            # which is pinned in pyproject.toml. If the import fails on
            # a GCP deployment, something is deeply wrong — surface it
            # loudly rather than silently accepting traffic.
            logger.error(
                "google-auth is not installed on a GCP worker instance; "
                "cannot verify OIDC tokens. Check Cloud Run image build.",
            )
            raise AuthenticationFailed("OIDC verification unavailable.")

        audience = self._get_expected_audience()
        if not audience:
            logger.error(
                "TASK_OIDC_AUDIENCE and WORKER_URL are both unset on a "
                "GCP worker; cannot verify OIDC token audience.",
            )
            raise AuthenticationFailed("OIDC audience not configured.")

        try:
            from google.auth import exceptions as google_auth_exceptions
            from google.oauth2 import id_token

            claims = id_token.verify_oauth2_token(
                token,
                transport,
                audience=audience,
            )
        except (google_auth_exceptions.TransportError, ValueError) as exc:
            # Signature invalid, audience mismatch, expired, malformed, or
            # JWKS fetch failed — all collapse into a 401.
            logger.warning("Worker OIDC verification failed: %s", exc)
            raise AuthenticationFailed(
                "Invalid OIDC identity token.",
            ) from exc

        return claims

    def _check_service_account_allowlist(self, claims: dict) -> None:
        """Reject tokens whose subject isn't an expected invoker SA.

        ``verify_oauth2_token`` only checks that the token was validly
        issued by Google. Without an allowlist, any Google service
        account with any audience would pass — we need to confirm the
        caller is specifically our Cloud Tasks invoker SA (or another
        explicitly-trusted identity).

        This also enforces that ``email_verified`` is ``True``, because
        an unverified email claim can't be trusted as an identity.
        """
        allowlist = self._get_allowlist()
        if not allowlist:
            # No allowlist configured is a deployment error, not an
            # attacker problem. Fail closed: reject with a clear message.
            logger.error(
                "Worker OIDC allowlist is empty; cannot authorise caller. "
                "Set TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS or "
                "CLOUD_TASKS_SERVICE_ACCOUNT.",
            )
            raise AuthenticationFailed(
                "OIDC allowlist not configured.",
            )

        email = (claims.get("email") or "").lower()
        email_verified = claims.get("email_verified", False)

        if not email_verified:
            logger.warning(
                "Worker OIDC token rejected: email_verified=false (email=%s)",
                email,
            )
            raise AuthenticationFailed("OIDC token email is not verified.")

        if email not in allowlist:
            logger.warning(
                "Worker OIDC token rejected: non-allowlisted SA (%s)",
                email,
            )
            raise AuthenticationFailed(
                "Service account not permitted to invoke worker.",
            )

    # -- Configuration helpers ---------------------------------------------

    @staticmethod
    def _get_expected_audience() -> str:
        """Expected ``aud`` claim — explicit setting, falling back to ``WORKER_URL``.

        Cloud Tasks signs tokens with ``audience=WORKER_URL`` (see
        :class:`validibot.core.tasks.dispatch.google_cloud_tasks.GoogleCloudTasksDispatcher`
        line ~124). Keeping these defaults aligned prevents silent 401
        drift the way the MCP audience drift (``api.validibot.com`` vs
        ``app.validibot.com``) did historically.
        """
        explicit = getattr(settings, "TASK_OIDC_AUDIENCE", "")
        if explicit:
            return explicit
        return getattr(settings, "WORKER_URL", "")

    @staticmethod
    def _get_allowlist() -> set[str]:
        """Lowercased set of allowed service-account emails.

        Priority:
        1. ``TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS`` (explicit list) wins.
        2. Otherwise, fall back to ``{CLOUD_TASKS_SERVICE_ACCOUNT}``,
           the service account the dispatcher uses to sign outgoing
           tokens (see ``GoogleCloudTasksDispatcher._get_invoker_service_account``).

        Emails are compared case-insensitively because the Google OIDC
        ``email`` claim is not case-canonical.
        """
        explicit = getattr(settings, "TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS", None)
        if explicit:
            return {email.lower() for email in explicit if email}

        fallback = getattr(settings, "CLOUD_TASKS_SERVICE_ACCOUNT", "")
        return {fallback.lower()} if fallback else set()

    @classmethod
    def _get_google_transport(cls):
        """Return a reusable google-auth HTTP transport, or ``None``.

        We cache the transport on the class (not the instance) because
        DRF instantiates a fresh authenticator per request. A
        per-instance transport would defeat connection pooling.

        ``None`` is returned when google-auth is not importable, which
        shouldn't happen on a GCP deployment (``google-cloud-tasks``
        pulls it in) but is handled defensively.
        """
        if not cls._google_transport_initialised:
            try:
                from google.auth.transport import requests as google_requests

                cls._google_transport = google_requests.Request()
            except ImportError:
                cls._google_transport = None
            cls._google_transport_initialised = True
        return cls._google_transport

    @classmethod
    def _reset_transport_cache(cls) -> None:
        """Drop the cached transport so tests can re-mock google-auth.

        Not for production use; only invoked by the test suite.
        """
        cls._google_transport = None
        cls._google_transport_initialised = False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_worker_auth_classes() -> list[type[BaseAuthentication]]:
    """Return DRF auth classes appropriate for the current DEPLOYMENT_TARGET.

    Called per request by
    :meth:`~validibot.core.api.worker.WorkerOnlyAPIView.get_authenticators`.
    The result is intentionally deployment-specific rather than layered:

    * On GCP, the **only** valid caller is Cloud Tasks / Cloud Scheduler,
      both of which always attach an OIDC token. Accepting
      :class:`WorkerKeyAuthentication` as a fallback would weaken the
      security posture (an attacker who stole the shared secret could
      bypass OIDC entirely), and ops staff can still ``curl`` the endpoint
      using ``gcloud auth print-identity-token`` when manual invocation
      is needed.

    * On Docker Compose / Celery deployments, no OIDC issuer exists —
      the shared-secret class remains the authoritative application-layer
      guard. This is unchanged from pre-fix behaviour.

    * The ``AWS`` target is not yet implemented; it falls back to
      :class:`WorkerKeyAuthentication` until an SQS/SNS signature
      verifier is added.

    Returns:
        An ordered list of authentication classes. DRF iterates through
        them in order, short-circuiting on the first that returns a
        non-``None`` result or raises ``AuthenticationFailed``.
    """
    target = get_deployment_target()

    if target == DeploymentTarget.GCP:
        return [CloudTasksOIDCAuthentication]

    # TEST / LOCAL_DOCKER_COMPOSE / DOCKER_COMPOSE / AWS → shared secret.
    # This preserves the exact pre-fix behaviour for every non-GCP path.
    return [WorkerKeyAuthentication]
