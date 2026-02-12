"""
Shared base classes for worker-only API endpoints.

Worker-only endpoints are deployed on the internal worker service and are called
by infrastructure components (task dispatchers, schedulers, validator containers).
The authentication mechanism varies by deployment:

- Docker Compose: Shared-secret API key (WORKER_API_KEY setting)
- GCP: Cloud Run IAM authenticates via OIDC tokens
- AWS: IAM-based authentication (future)

When WORKER_API_KEY is set, callers must include it in the Authorization header::

    Authorization: Worker-Key <key>

When WORKER_API_KEY is not set (GCP path), the key check is skipped and
infrastructure-level auth is relied upon instead.
"""

from django.conf import settings
from django.http import Http404
from rest_framework.views import APIView

from validibot.core.api.worker_auth import WorkerKeyAuthentication


class WorkerOnlyAPIView(APIView):
    """
    Base class for internal worker-only endpoints.

    Security is layered:
    1. URL routing: worker endpoints only exist on worker instances (urls_worker.py)
    2. App guard: returns 404 on non-worker instances (defense in depth)
    3. API key: WORKER_API_KEY checked via WorkerKeyAuthentication (Docker Compose)
    4. Infrastructure: Cloud Run IAM / network isolation (GCP)
    """

    authentication_classes = [WorkerKeyAuthentication]
    permission_classes = []

    def initial(self, request, *args, **kwargs):
        """Ensure worker-only endpoints don't respond on web instances."""
        if not getattr(settings, "APP_IS_WORKER", False):
            raise Http404
        super().initial(request, *args, **kwargs)
