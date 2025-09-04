from datetime import timedelta

import django_filters
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import permissions
from rest_framework import viewsets

from roscoe.validations.constants import ValidationRunStatus
from roscoe.validations.models import ValidationRun
from roscoe.validations.serializers import ValidationRunSerializer


class ValidationRunFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ValidationRunStatus.choices)
    workflow = django_filters.NumberFilter()
    submission = django_filters.NumberFilter()
    after = django_filters.DateFilter(field_name="created", lookup_expr="gte")
    before = django_filters.DateFilter(field_name="created", lookup_expr="lte")
    on = django_filters.DateFilter(field_name="created", lookup_expr="date")

    class Meta:
        model = ValidationRun
        fields = []  # explicit filters above


def _truthy(value: str | None) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


class ValidationRunViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ValidationRun.objects.all()
    serializer_class = ValidationRunSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = ValidationRunFilter
    ordering_fields = ["created", "id", "status"]
    ordering = ["-created", "-id"]
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        # Scope to current org only
        org = getattr(self.request.user, "get_current_org", lambda: None)()
        qs = (
            super().get_queryset().filter(org=org)
            if org
            else ValidationRun.objects.none()
        )

        # Default recent-only (last 30 days) unless:
        # - ?all=1 provided, or
        # - any explicit date filter (after/before/on) provided.
        qp = self.request.query_params
        has_explicit_dates = any(k in qp for k in ("after", "before", "on"))
        if not _truthy(qp.get("all")) and not has_explicit_dates:
            cutoff = timezone.now() - timedelta(days=30)
            qs = qs.filter(created__gte=cutoff)

        return qs
