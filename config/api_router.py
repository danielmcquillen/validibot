from django.conf import settings
from rest_framework.routers import DefaultRouter, SimpleRouter

from roscoe.users.api.views import UserViewSet
from roscoe.validations.views import ValidationRunViewSet
from roscoe.workflows.views import WorkflowViewSet

router = DefaultRouter() if settings.DEBUG else SimpleRouter()

router.register("users", UserViewSet)
router.register("workflows", WorkflowViewSet, basename="workflow")
router.register("validation-runs", ValidationRunViewSet, basename="validation-runs")

app_name = "api"
urlpatterns = router.urls
