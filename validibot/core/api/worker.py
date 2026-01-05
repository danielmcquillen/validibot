"""
Shared base classes for worker-only API endpoints.

Worker-only endpoints are deployed on the internal worker service and are called
by infrastructure components such as Cloud Tasks, Cloud Scheduler, and validator
Cloud Run Jobs. Cloud Run IAM authenticates these requests before they reach
Django, so Django REST Framework authentication is disabled for these views.
"""

from django.conf import settings
from django.http import Http404
from rest_framework.views import APIView


class WorkerOnlyAPIView(APIView):
    """
    Base class for internal worker-only endpoints.

    This adds a defense-in-depth guard that returns 404 when the code is running
    on a non-worker instance, even if URL routing is misconfigured. Authentication
    and authorization are enforced by Cloud Run IAM, so DRF auth is disabled.
    """

    authentication_classes = []
    permission_classes = []

    def initial(self, request, *args, **kwargs):
        """Ensure worker-only endpoints don't respond on web instances."""
        if not getattr(settings, "APP_IS_WORKER", False):
            raise Http404
        super().initial(request, *args, **kwargs)

