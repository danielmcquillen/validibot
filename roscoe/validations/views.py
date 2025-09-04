import django_filters
from django.conf import settings
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import permissions
from rest_framework import status
from rest_framework import viewsets
from rest_framework.response import Response

from roscoe.validations.constants import ValidationRunStatus
from roscoe.validations.models import ValidationRun
from roscoe.validations.serializers import ValidationRunSerializer


class ValidationRunFilter(django_filters.FilterSet):
    class Meta:
        model = ValidationRun
        fields = []  # We define filters explicitly above

    status = django_filters.ChoiceFilter(choices=ValidationRunStatus.choices)
    workflow = django_filters.NumberFilter()
    submission = django_filters.NumberFilter()
    after = django_filters.DateFilter(field_name="created", lookup_expr="gte")
    before = django_filters.DateFilter(field_name="created", lookup_expr="lte")
    on = django_filters.DateFilter(field_name="created", lookup_expr="date")


class ValidationRunViewSet(viewsets.ModelViewSet):
    queryset = ValidationRun.objects.all()
    serializer_class = ValidationRunSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = ValidationRunFilter
    ordering_fields = ["created", "id", "status"]
    ordering = ["-created", "-id"]

    def get_queryset(self):
        current_org = self.request.user.get_current_org()
        return super().get_queryset().filter(org=current_org)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        data = ValidationRunSerializer(instance).data
        if instance.status in (
            ValidationRunStatus.SUCCEEDED,
            ValidationRunStatus.FAILED,
            getattr(ValidationRunStatus, "CANCELED", "canceled"),
            getattr(ValidationRunStatus, "TIMED_OUT", "timed_out"),
        ):
            return Response(data, status=status.HTTP_200_OK)
        return Response(
            data,
            status=status.HTTP_202_ACCEPTED,
            headers={
                "Retry-After": str(
                    getattr(settings, "VALIDATION_START_ATTEMPT_TIMEOUT", 5),
                ),
            },
        )
