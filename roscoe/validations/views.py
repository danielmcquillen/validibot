from datetime import timedelta

import django_filters
from django.conf import settings
from django.utils import timezone
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
    status = django_filters.ChoiceFilter(choices=ValidationRunStatus.choices)
    workflow = django_filters.NumberFilter()
    submission = django_filters.NumberFilter()
    after = django_filters.DateFilter(field_name="created", lookup_expr="gte")
    before = django_filters.DateFilter(field_name="created", lookup_expr="lte")
    on = django_filters.DateFilter(field_name="created", lookup_expr="date")

    # Pass ?all=1 to disable the default “recent only” window
    all = django_filters.BooleanFilter(method="filter_all", label="All records")

    class Meta:
        model = ValidationRun
        fields = []  # filters defined explicitly above

    def filter_all(self, queryset, name, value):
        # No-op; we only read this flag in qs below to decide default windowing.
        return queryset

    @property
    def qs(self):
        """
        Default to last 30 days unless caller specifies any date filter or ?all=1.
        Keeping this logic here keeps get_queryset() simple/DRF-idiomatic.
        """
        qs = super().qs
        form = getattr(self, "form", None)
        cleaned = getattr(form, "cleaned_data", None)
        if not cleaned:
            return qs

        has_explicit_dates = bool(
            cleaned.get("after") or cleaned.get("before") or cleaned.get("on")
        )
        show_all = bool(cleaned.get("all"))

        if not has_explicit_dates and not show_all:
            cutoff = timezone.now() - timedelta(days=30)
            qs = qs.filter(created__gte=cutoff)

        return qs


class ValidationRunViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ValidationRun.objects.all()
    serializer_class = ValidationRunSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = ValidationRunFilter
    ordering_fields = ["created", "id", "status"]
    ordering = ["-created", "-id"]
    http_method_names = ["get", "head", "options"]  # explicit

    def get_queryset(self):
        current_org = self.request.user.get_current_org()
        return super().get_queryset().filter(org=current_org)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        data = ValidationRunSerializer(instance).data
        return Response(
            data,
            status=status.HTTP_200_OK,
            headers={
                "Retry-After": str(
                    getattr(settings, "VALIDATION_START_ATTEMPT_TIMEOUT", 5),
                ),
            },
        )
