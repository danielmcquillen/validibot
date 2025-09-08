from django.conf import settings
from rest_framework.routers import DefaultRouter
from rest_framework.routers import SimpleRouter

from roscoe.users.api.views import UserViewSet
from roscoe.validations.views import ValidationRunViewSet
from roscoe.workflows.views import WorkflowViewSet

router = DefaultRouter() if settings.DEBUG else SimpleRouter()

router.register("users", UserViewSet)
router.register("workflows", WorkflowViewSet)
router.register("validation-runs", ValidationRunViewSet)

app_name = "api"
urlpatterns = router.urls
