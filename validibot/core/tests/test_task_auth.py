"""
Tests for platform-agnostic worker-endpoint authentication.

This suite covers :mod:`validibot.core.api.task_auth`, which selects and
enforces the correct application-layer authentication for worker-only
endpoints based on ``DEPLOYMENT_TARGET``.

Why this matters
----------------
Worker endpoints (validation execute, callbacks, scheduled tasks) are
invoked by infrastructure — Cloud Tasks on GCP, Celery over Docker bridge
networks elsewhere. An application-layer auth regression would either:

* Break legitimate infrastructure traffic (hard outage), or
* Open the endpoints to unauthenticated callers (silent security
  vulnerability — IAM misconfiguration or a shared ingress rule is all
  that stands between the attacker and arbitrary validation dispatch).

Each test here pins one specific invariant of the auth contract so that a
regression surfaces as a named failure rather than a mysterious 401/403
in staging.
"""

from __future__ import annotations

from unittest import mock

import pytest
from django.test import RequestFactory
from django.test import override_settings
from rest_framework.exceptions import AuthenticationFailed

from validibot.core.api.task_auth import CloudTasksOIDCAuthentication
from validibot.core.api.task_auth import WorkerIdentity
from validibot.core.api.task_auth import get_worker_auth_classes
from validibot.core.api.worker_auth import WorkerKeyAuthentication

# ---------------------------------------------------------------------------
# Factory: DEPLOYMENT_TARGET → auth classes
# ---------------------------------------------------------------------------
#
# The factory is the single source of truth for "which backend runs where".
# These tests pin the mapping so that an accidental edit to the factory
# (e.g., removing OIDC on GCP) shows up as an explicit test failure.


class TestGetWorkerAuthClasses:
    """Verify the factory returns the correct backends per deployment target."""

    @override_settings(DEPLOYMENT_TARGET="gcp")
    def test_gcp_returns_oidc_only(self):
        """GCP deployments must enforce OIDC verification exclusively.

        Falling back to shared-secret on GCP would let an attacker who
        stole ``WORKER_API_KEY`` bypass OIDC entirely; ops staff can
        still invoke manually with ``gcloud auth print-identity-token``
        when needed, so there is no legitimate reason to accept the
        shared secret here.
        """
        classes = get_worker_auth_classes()
        assert classes == [CloudTasksOIDCAuthentication]

    @override_settings(DEPLOYMENT_TARGET="docker_compose")
    def test_docker_compose_returns_shared_secret(self):
        """Docker Compose must keep ``WorkerKeyAuthentication`` unchanged.

        There is no OIDC issuer on Docker Compose, so the shared secret
        is the only available app-layer guard against SSRF between
        containers that share the Docker bridge network.
        """
        classes = get_worker_auth_classes()
        assert classes == [WorkerKeyAuthentication]

    @override_settings(DEPLOYMENT_TARGET="local_docker_compose")
    def test_local_docker_compose_returns_shared_secret(self):
        """Local Docker Compose inherits the production Docker Compose path.

        Keeping local and prod identical for this deployment target means
        auth bugs are found during local development, not in production.
        """
        classes = get_worker_auth_classes()
        assert classes == [WorkerKeyAuthentication]

    @override_settings(DEPLOYMENT_TARGET="test")
    def test_test_target_returns_shared_secret(self):
        """The test deployment target must use shared secret.

        The existing ``test_worker_auth.py`` suite relies on this: it
        overrides ``WORKER_API_KEY`` and expects that setting alone to
        control auth. Switching the test path to OIDC would silently
        break that whole suite.
        """
        classes = get_worker_auth_classes()
        assert classes == [WorkerKeyAuthentication]

    @override_settings(DEPLOYMENT_TARGET="aws")
    def test_aws_placeholder_returns_shared_secret(self):
        """AWS is a declared target with no dispatcher yet; it must not crash.

        Until the AWS execution backend lands (SQS + SNS signatures), we
        fail safely by using the shared-secret path. The moment AWS gains
        its own backend this assertion will need updating in lockstep.
        """
        classes = get_worker_auth_classes()
        assert classes == [WorkerKeyAuthentication]


# ---------------------------------------------------------------------------
# CloudTasksOIDCAuthentication — happy path
# ---------------------------------------------------------------------------
#
# We mock ``google.oauth2.id_token.verify_oauth2_token`` because it would
# otherwise perform a real network call to Google's JWKS endpoint. The
# signature check itself is exercised by google-auth's own tests; our
# concern is "given a successful verification, do we make the correct
# application-layer decisions about audience, email_verified, and the
# SA allowlist?".


@pytest.fixture(autouse=True)
def _reset_oidc_transport_cache():
    """Reset the module-level google-auth transport cache between tests.

    The transport is cached on the class to reuse TCP connections in
    production. For tests, we need a clean slate so that patching
    google-auth in one test doesn't leak into another.
    """
    CloudTasksOIDCAuthentication._reset_transport_cache()
    yield
    CloudTasksOIDCAuthentication._reset_transport_cache()


def _make_request(authorization: str | None = None):
    """Construct a DRF-compatible request with an optional Authorization header."""
    factory = RequestFactory()
    headers = {}
    if authorization is not None:
        headers["HTTP_AUTHORIZATION"] = authorization
    return factory.post("/api/v1/execute-validation-run/", **headers)


class TestCloudTasksOIDCAuthenticationHappyPath:
    """A valid Cloud Tasks OIDC token must pass end-to-end."""

    @override_settings(
        TASK_OIDC_AUDIENCE="https://worker.example.com",
        TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=["invoker@proj.iam.gserviceaccount.com"],
    )
    def test_valid_token_from_allowlisted_sa_authenticates(self):
        """The happy path: valid signature + correct audience + allowlisted SA.

        This is the hot path in production. If this test fails, Cloud
        Tasks traffic can't execute validation runs in production, which
        means the app is broken for every customer.
        """
        # Stub google-auth so the auth class thinks the library is
        # present but all calls are under our control.
        CloudTasksOIDCAuthentication._google_transport = object()
        CloudTasksOIDCAuthentication._google_transport_initialised = True

        claims = {
            "email": "invoker@proj.iam.gserviceaccount.com",
            "email_verified": True,
            "aud": "https://worker.example.com",
        }
        auth = CloudTasksOIDCAuthentication()
        request = _make_request(authorization="Bearer real-oidc-token")

        with mock.patch(
            "google.oauth2.id_token.verify_oauth2_token",
            return_value=claims,
        ):
            user, identity = auth.authenticate(request)

        assert user is None, "Worker endpoints never resolve to a Django user."
        assert isinstance(identity, WorkerIdentity)
        assert identity.source == "cloud_tasks_oidc"
        assert identity.email == "invoker@proj.iam.gserviceaccount.com"

    @override_settings(
        TASK_OIDC_AUDIENCE="",  # empty explicit setting
        WORKER_URL="https://worker.example.com",
        TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=["invoker@proj.iam.gserviceaccount.com"],
    )
    def test_audience_falls_back_to_worker_url(self):
        """``TASK_OIDC_AUDIENCE`` empty must fall back to ``WORKER_URL``.

        The GCP dispatcher signs tokens with ``audience=WORKER_URL``. The
        MCP equivalent historically drifted (api.validibot.com vs
        app.validibot.com) and caused silent 401s; this test pins the
        default so the same bug can't happen here.
        """
        CloudTasksOIDCAuthentication._google_transport = object()
        CloudTasksOIDCAuthentication._google_transport_initialised = True

        claims = {
            "email": "invoker@proj.iam.gserviceaccount.com",
            "email_verified": True,
            "aud": "https://worker.example.com",
        }
        auth = CloudTasksOIDCAuthentication()
        request = _make_request(authorization="Bearer token")

        with mock.patch(
            "google.oauth2.id_token.verify_oauth2_token",
            return_value=claims,
        ) as verify:
            auth.authenticate(request)

        # Confirm verify_oauth2_token was called with the WORKER_URL audience
        _args, kwargs = verify.call_args
        assert kwargs["audience"] == "https://worker.example.com"

    @override_settings(
        TASK_OIDC_AUDIENCE="https://worker.example.com",
        TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=[],  # not set
        CLOUD_TASKS_SERVICE_ACCOUNT="invoker@proj.iam.gserviceaccount.com",
    )
    def test_allowlist_falls_back_to_cloud_tasks_sa(self):
        """An empty allowlist must fall back to ``{CLOUD_TASKS_SERVICE_ACCOUNT}``.

        Single-SA GCP deployments (the default) shouldn't need to
        specify the SA twice. The dispatcher and the verifier both
        derive their SA from ``CLOUD_TASKS_SERVICE_ACCOUNT`` so they
        stay aligned by construction.
        """
        CloudTasksOIDCAuthentication._google_transport = object()
        CloudTasksOIDCAuthentication._google_transport_initialised = True

        claims = {
            "email": "invoker@proj.iam.gserviceaccount.com",
            "email_verified": True,
            "aud": "https://worker.example.com",
        }
        auth = CloudTasksOIDCAuthentication()
        request = _make_request(authorization="Bearer token")

        with mock.patch(
            "google.oauth2.id_token.verify_oauth2_token",
            return_value=claims,
        ):
            user, identity = auth.authenticate(request)
        assert identity.email == "invoker@proj.iam.gserviceaccount.com"

    @override_settings(
        TASK_OIDC_AUDIENCE="https://worker.example.com",
        TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=["Invoker@Proj.Iam.GServiceAccount.Com"],
    )
    def test_allowlist_comparison_is_case_insensitive(self):
        """SA email comparison must be case-insensitive.

        Google's OIDC ``email`` claim isn't case-canonical. A case
        mismatch between the configured allowlist and the actual token
        claim would silently reject all production traffic.
        """
        CloudTasksOIDCAuthentication._google_transport = object()
        CloudTasksOIDCAuthentication._google_transport_initialised = True

        claims = {
            "email": "invoker@proj.iam.gserviceaccount.com",
            "email_verified": True,
            "aud": "https://worker.example.com",
        }
        auth = CloudTasksOIDCAuthentication()
        request = _make_request(authorization="Bearer token")

        with mock.patch(
            "google.oauth2.id_token.verify_oauth2_token",
            return_value=claims,
        ):
            user, _ = auth.authenticate(request)
        assert user is None  # success


# ---------------------------------------------------------------------------
# CloudTasksOIDCAuthentication — rejection paths
# ---------------------------------------------------------------------------
#
# Each of these is a specific attacker/misconfiguration scenario. If any
# of them stops raising, the endpoint has become exploitable.


class TestCloudTasksOIDCAuthenticationRejections:
    """Verify every failure mode raises :class:`AuthenticationFailed`."""

    @override_settings(
        TASK_OIDC_AUDIENCE="https://worker.example.com",
        TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=["invoker@proj.iam.gserviceaccount.com"],
    )
    def test_missing_authorization_header_rejected(self):
        """No ``Authorization`` header → reject.

        Cloud Tasks always sends Bearer tokens, so a missing header
        means either a misconfigured client or an attacker probing the
        endpoint. There is no legitimate unauthenticated path on GCP.
        """
        auth = CloudTasksOIDCAuthentication()
        request = _make_request()
        with pytest.raises(AuthenticationFailed, match="Missing OIDC identity token"):
            auth.authenticate(request)

    @override_settings(
        TASK_OIDC_AUDIENCE="https://worker.example.com",
        TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=["invoker@proj.iam.gserviceaccount.com"],
    )
    def test_non_bearer_authorization_rejected(self):
        """A non-Bearer scheme (e.g., ``Worker-Key``) must be rejected.

        On GCP the only acceptable scheme is Bearer; anything else means
        the request isn't coming from Cloud Tasks/Scheduler.
        """
        auth = CloudTasksOIDCAuthentication()
        request = _make_request(authorization="Worker-Key abc123")
        with pytest.raises(AuthenticationFailed, match="Missing OIDC identity token"):
            auth.authenticate(request)

    @override_settings(
        TASK_OIDC_AUDIENCE="https://worker.example.com",
        TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=["invoker@proj.iam.gserviceaccount.com"],
    )
    def test_invalid_signature_rejected(self):
        """A token that fails signature verification must be rejected.

        google-auth raises ``ValueError`` on signature failures; the
        auth class must translate that into ``AuthenticationFailed``
        rather than letting it escape as a 500.
        """
        CloudTasksOIDCAuthentication._google_transport = object()
        CloudTasksOIDCAuthentication._google_transport_initialised = True

        auth = CloudTasksOIDCAuthentication()
        request = _make_request(authorization="Bearer tampered-token")

        with (
            mock.patch(
                "google.oauth2.id_token.verify_oauth2_token",
                side_effect=ValueError("Invalid signature"),
            ),
            pytest.raises(
                AuthenticationFailed,
                match="Invalid OIDC identity token",
            ),
        ):
            auth.authenticate(request)

    @override_settings(
        TASK_OIDC_AUDIENCE="https://worker.example.com",
        TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=["invoker@proj.iam.gserviceaccount.com"],
    )
    def test_audience_mismatch_rejected(self):
        """An audience mismatch must be rejected.

        ``verify_oauth2_token`` raises ``ValueError`` when the token's
        ``aud`` differs from the expected audience. This guards against
        an attacker replaying a legitimately-issued token aimed at a
        different service (e.g., a Cloud Run Job's callback URL).
        """
        CloudTasksOIDCAuthentication._google_transport = object()
        CloudTasksOIDCAuthentication._google_transport_initialised = True

        auth = CloudTasksOIDCAuthentication()
        request = _make_request(authorization="Bearer wrong-audience-token")

        with (
            mock.patch(
                "google.oauth2.id_token.verify_oauth2_token",
                side_effect=ValueError("Token has wrong audience"),
            ),
            pytest.raises(
                AuthenticationFailed,
                match="Invalid OIDC identity token",
            ),
        ):
            auth.authenticate(request)

    @override_settings(
        TASK_OIDC_AUDIENCE="https://worker.example.com",
        TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=["invoker@proj.iam.gserviceaccount.com"],
    )
    def test_email_not_verified_rejected(self):
        """Tokens with ``email_verified=false`` must be rejected.

        An unverified ``email`` claim can't be trusted as an identity;
        we must not match it against the allowlist. In practice Google
        always sets ``email_verified=true`` for SA tokens, but defending
        against ``false`` here means a future change to the claim
        semantics can't silently weaken us.
        """
        CloudTasksOIDCAuthentication._google_transport = object()
        CloudTasksOIDCAuthentication._google_transport_initialised = True

        claims = {
            "email": "invoker@proj.iam.gserviceaccount.com",
            "email_verified": False,
            "aud": "https://worker.example.com",
        }
        auth = CloudTasksOIDCAuthentication()
        request = _make_request(authorization="Bearer token")

        with (
            mock.patch(
                "google.oauth2.id_token.verify_oauth2_token",
                return_value=claims,
            ),
            pytest.raises(
                AuthenticationFailed,
                match="email is not verified",
            ),
        ):
            auth.authenticate(request)

    @override_settings(
        TASK_OIDC_AUDIENCE="https://worker.example.com",
        TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=["allowed@proj.iam.gserviceaccount.com"],
    )
    def test_non_allowlisted_service_account_rejected(self):
        """A verified-but-wrong service account must be rejected.

        ``verify_oauth2_token`` confirms *Google* signed the token, but
        any Google user or service account with any audience would
        satisfy it. Without the allowlist, an attacker who tricked any
        GCP tenant into calling our endpoint with any audience could
        impersonate legitimate traffic. The allowlist closes that loop.
        """
        CloudTasksOIDCAuthentication._google_transport = object()
        CloudTasksOIDCAuthentication._google_transport_initialised = True

        claims = {
            "email": "attacker@evil.iam.gserviceaccount.com",
            "email_verified": True,
            "aud": "https://worker.example.com",
        }
        auth = CloudTasksOIDCAuthentication()
        request = _make_request(authorization="Bearer token")

        with (
            mock.patch(
                "google.oauth2.id_token.verify_oauth2_token",
                return_value=claims,
            ),
            pytest.raises(
                AuthenticationFailed,
                match="not permitted to invoke worker",
            ),
        ):
            auth.authenticate(request)

    @override_settings(
        TASK_OIDC_AUDIENCE="https://worker.example.com",
        TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=[],
        CLOUD_TASKS_SERVICE_ACCOUNT="",  # no fallback configured
    )
    def test_empty_allowlist_rejected(self):
        """An empty allowlist must fail closed, not open.

        If neither ``TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS`` nor
        ``CLOUD_TASKS_SERVICE_ACCOUNT`` is set, we have no basis to
        authorise any caller. The right response is to reject all
        traffic and log loudly — a deployment error, not an attacker
        problem.
        """
        CloudTasksOIDCAuthentication._google_transport = object()
        CloudTasksOIDCAuthentication._google_transport_initialised = True

        claims = {
            "email": "invoker@proj.iam.gserviceaccount.com",
            "email_verified": True,
            "aud": "https://worker.example.com",
        }
        auth = CloudTasksOIDCAuthentication()
        request = _make_request(authorization="Bearer token")

        with (
            mock.patch(
                "google.oauth2.id_token.verify_oauth2_token",
                return_value=claims,
            ),
            pytest.raises(
                AuthenticationFailed,
                match="allowlist not configured",
            ),
        ):
            auth.authenticate(request)

    @override_settings(
        TASK_OIDC_AUDIENCE="",
        WORKER_URL="",  # both unset
        TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=["invoker@proj.iam.gserviceaccount.com"],
    )
    def test_missing_audience_config_rejected(self):
        """No configured audience must fail closed.

        Calling ``verify_oauth2_token`` with ``audience=""`` would
        disable the audience check — that's a silent security hole. We
        detect the empty-string case explicitly and refuse to proceed.
        """
        CloudTasksOIDCAuthentication._google_transport = object()
        CloudTasksOIDCAuthentication._google_transport_initialised = True

        auth = CloudTasksOIDCAuthentication()
        request = _make_request(authorization="Bearer token")
        with pytest.raises(
            AuthenticationFailed,
            match="audience not configured",
        ):
            auth.authenticate(request)

    def test_missing_google_auth_library_rejected(self):
        """If google-auth isn't installed, we must fail closed.

        In practice ``google-cloud-tasks`` pulls in google-auth, so this
        should never happen on a real GCP deployment — but if the image
        build is misconfigured, silently accepting traffic would be
        catastrophic.
        """
        # Force the transport to behave as if google-auth is absent.
        CloudTasksOIDCAuthentication._google_transport = None
        CloudTasksOIDCAuthentication._google_transport_initialised = True

        with override_settings(
            TASK_OIDC_AUDIENCE="https://worker.example.com",
            TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=[
                "invoker@proj.iam.gserviceaccount.com",
            ],
        ):
            auth = CloudTasksOIDCAuthentication()
            request = _make_request(authorization="Bearer token")
            with pytest.raises(
                AuthenticationFailed,
                match="OIDC verification unavailable",
            ):
                auth.authenticate(request)


# ---------------------------------------------------------------------------
# authenticate_header
# ---------------------------------------------------------------------------


def test_authenticate_header_advertises_bearer_challenge():
    """DRF uses ``authenticate_header()`` to build the ``WWW-Authenticate`` reply.

    A 401 without a ``WWW-Authenticate`` header is not RFC-compliant and
    breaks well-behaved generic clients. This test pins the challenge
    format so it doesn't silently drift.
    """
    auth = CloudTasksOIDCAuthentication()
    challenge = auth.authenticate_header(_make_request())
    assert "Bearer" in challenge
    assert "validibot-worker" in challenge
