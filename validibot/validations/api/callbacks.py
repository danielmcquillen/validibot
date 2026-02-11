"""
Callback API endpoint for container-based validators.

Validator containers (EnergyPlus, FMI) POST completion callbacks to the worker
service when they finish. This API view is intentionally thin and delegates
processing to ValidationCallbackService.

Security is enforced at the infrastructure level (e.g., Cloud Run IAM, network
isolation). This endpoint also includes a defense-in-depth guard (WorkerOnlyAPIView)
to return 404 on non-worker instances.
"""

from validibot.core.api.worker import WorkerOnlyAPIView
from validibot.validations.services.validation_callback import ValidationCallbackService


class ValidationCallbackView(WorkerOnlyAPIView):
    """
    Handle validation completion callbacks from validator containers.

    This view delegates all parsing, idempotency, and persistence logic to
    ValidationCallbackService so the HTTP layer stays small and consistent.
    """

    def post(self, request):
        """Process a validator callback payload."""
        return ValidationCallbackService().process(payload=request.data)
