"""
Shared base class for worker-only API endpoints.

Worker-only endpoints are deployed on the internal worker service and are
called by infrastructure components (task dispatchers, schedulers,
validator containers). The authentication mechanism is platform-dependent
and selected automatically based on ``DEPLOYMENT_TARGET``:

* **Docker Compose / Celery** — shared-secret API key
  (``WORKER_API_KEY``), via :class:`WorkerKeyAuthentication`.
* **GCP (Cloud Run + Cloud Tasks)** — Google-signed OIDC identity tokens,
  via :class:`CloudTasksOIDCAuthentication`. Cloud Run IAM remains the
  primary infrastructure control; OIDC verification at the application
  layer provides defence in depth against IAM misconfiguration.
* **AWS** — not yet implemented (currently falls back to shared secret).

The mapping lives in
:func:`validibot.core.api.task_auth.get_worker_auth_classes`. Adding a
new endpoint = subclass this view, nothing else to configure.

Security is layered:

1. **URL routing** — worker endpoints only exist on worker instances
   (``config/urls_worker.py``).
2. **App guard** — :meth:`initial` raises ``Http404`` on non-worker
   instances (defence in depth, runs before auth).
3. **Application-layer auth** — platform-specific class chosen by
   ``DEPLOYMENT_TARGET`` (this module).
4. **Infrastructure auth** — Cloud Run IAM (GCP), Docker network
   isolation (Docker Compose). Always the primary control.
"""

from __future__ import annotations

from django.conf import settings
from django.http import Http404
from rest_framework.views import APIView


class WorkerOnlyAPIView(APIView):
    """Base class for internal worker-only endpoints.

    Authentication is resolved dynamically per request via
    :meth:`get_authenticators`, which consults
    :func:`validibot.core.api.task_auth.get_worker_auth_classes` so the
    right backend is chosen for the current ``DEPLOYMENT_TARGET``.

    Subclasses don't need to override ``authentication_classes``; the
    per-request factory is authoritative. If a subclass needs to *add*
    authentication (unusual), it should extend the list returned by
    ``super().get_authenticators()`` rather than replacing it.
    """

    # Permission-by-policy: every worker endpoint is infrastructure-only,
    # so there is no Django permission to enforce beyond authentication.
    permission_classes: list = []

    def initial(self, request, *args, **kwargs):
        """Reject requests that land on a web (non-worker) instance.

        ``APP_IS_WORKER`` is set by the deployment layer
        (``APP_ROLE=worker``). Running this check before authentication
        means probing the endpoint from a web instance returns 404 with
        no information about which auth scheme is expected — small
        information-disclosure hardening.
        """
        if not getattr(settings, "APP_IS_WORKER", False):
            raise Http404
        super().initial(request, *args, **kwargs)

    def get_authenticators(self):
        """Return platform-appropriate authenticator instances.

        Overrides DRF's default, which instantiates
        ``self.authentication_classes``. We consult the deployment-aware
        factory instead so a new ``DEPLOYMENT_TARGET`` can grow its own
        backend (e.g., AWS SQS HTTP signatures) without touching this
        class or any worker view.

        Called once per request by DRF.
        """
        # Imported here to avoid a module-level import cycle:
        # task_auth imports WorkerKeyAuthentication, which lives in the
        # same ``validibot.core.api`` package.
        from validibot.core.api.task_auth import get_worker_auth_classes

        return [cls() for cls in get_worker_auth_classes()]
