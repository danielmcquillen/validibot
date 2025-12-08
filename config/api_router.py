"""
Public API router (available on APP_ROLE=web).

These routes are the regular API surface used by clients to launch workflows,
manage users, etc. Internal-only endpoints (e.g., validator callbacks) live in
config/api_internal_router.py and are exposed only on the worker service.
"""

from django.conf import settings
from rest_framework.routers import DefaultRouter
from rest_framework.routers import SimpleRouter

from validibot.users.api.views import UserViewSet
from validibot.validations.views import ValidationRunViewSet
from validibot.workflows.views import WorkflowViewSet

router = DefaultRouter() if settings.DEBUG else SimpleRouter()

router.register("users", UserViewSet)
router.register("workflows", WorkflowViewSet, basename="workflow")
router.register("validation-runs", ValidationRunViewSet, basename="validation-runs")

app_name = "api"
urlpatterns = [
    *router.urls,
    # Public API endpoints only. Validator callbacks are internal-only.
]
