from datetime import timedelta

import django_filters
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import permissions
from rest_framework import viewsets

from roscoe.validations.constants import JobStatus
from roscoe.validations.models import ValidationRun
from roscoe.validations.serializers import ValidationRunSerializer


class ValidationRunFilter(django_filters.FilterSet):
    class Meta:
        model = ValidationRun
        fields = []  # We define filters explicitly above

    status = django_filters.ChoiceFilter(choices=JobStatus.choices)
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
        """
        Gets validation runs for the current user's organization.

        Returns only recents runs by default, unless the client specifies
        a time filter or explicitly asks for all.

        Returns:
            _type_: _description_
        """
        qs = super().get_queryset()
        current_org = self.request.user.get_current_org()
        qs = qs.filter(org=current_org)

        params = self.request.query_params
        has_time_filter = any(
            key in params
            for key in (
                "after",  # matches our custom filter names
                "before",  # matches our custom filter names
                "on",  # matches our custom filter names
                "cursor",  # pagination cursor means they're browsing
                "page",  # page-based pagination
            )
        )
        if not has_time_filter and params.get("all") not in ("1", "true", "True"):
            qs = qs.filter(created__gte=timezone.now() - timedelta(days=30))
        return qs
