from django.conf import settings
from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework.routers import SimpleRouter

from simplevalidations.users.api.views import UserViewSet
from simplevalidations.validations.api.callbacks import ValidationCallbackView
from simplevalidations.validations.views import ValidationRunViewSet
from simplevalidations.workflows.views import WorkflowViewSet

router = DefaultRouter() if settings.DEBUG else SimpleRouter()

router.register("users", UserViewSet)
router.register("workflows", WorkflowViewSet, basename="workflow")
router.register("validation-runs", ValidationRunViewSet, basename="validation-runs")

app_name = "api"
urlpatterns = router.urls + [
    path(
        "validation-callbacks/",
        ValidationCallbackView.as_view(),
        name="validation-callbacks",
    ),
]
