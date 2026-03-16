"""DRF viewset and filterset for ValidationRun API access."""

import datetime as dt
import logging
from datetime import timedelta

import django_filters
from django.db.models import Prefetch
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import permissions
from rest_framework import viewsets

from validibot.core.utils import truthy
from validibot.users.permissions import PermissionCode
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.serializers import ValidationRunSerializer

logger = logging.getLogger(__name__)


class ValidationRunFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ValidationRunStatus.choices)
    workflow = django_filters.NumberFilter()
    submission = django_filters.NumberFilter()
    after = django_filters.DateFilter(field_name="created", method="filter_after")
    before = django_filters.DateFilter(field_name="created", method="filter_before")
    on = django_filters.DateFilter(field_name="created", lookup_expr="date")

    def filter_after(self, queryset, name, value):
        """Filter runs created on or after the given date (timezone-aware)."""
        aware_dt = dt.datetime.combine(value, dt.time.min, tzinfo=dt.UTC)
        return queryset.filter(created__gte=aware_dt)

    def filter_before(self, queryset, name, value):
        """Filter runs created on or before the given date (timezone-aware)."""
        aware_dt = dt.datetime.combine(value, dt.time.max, tzinfo=dt.UTC)
        return queryset.filter(created__lte=aware_dt)

    class Meta:
        model = ValidationRun
        fields = []  # explicit filters above


class ValidationRunViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ValidationRun.objects.all()
    serializer_class = ValidationRunSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = ValidationRunFilter
    ordering_fields = ["created", "id", "status"]
    ordering = ["-created", "-id"]
    http_method_names = ["get", "head", "options"]

    def _active_membership(self, user):
        active_org = getattr(self.request, "active_org", None)
        active_org_id = (
            active_org.id if active_org else getattr(user, "current_org_id", None)
        )
        if not active_org_id:
            return None, None
        membership = (
            user.memberships.filter(org_id=active_org_id, is_active=True)
            .select_related("org")
            .prefetch_related("membership_roles__role")
            .first()
        )
        return membership, active_org_id

    def _access_context(self):
        """
        Resolve the active membership and permission flags for the current user.
        """

        user = self.request.user
        membership, active_org_id = self._active_membership(user)
        if not membership or not active_org_id:
            return None, None, False, False
        org = membership.org
        has_full_access = user.has_perm(
            PermissionCode.VALIDATION_RESULTS_VIEW_ALL.value,
            org,
        )
        has_own_access = user.has_perm(
            PermissionCode.VALIDATION_RESULTS_VIEW_OWN.value,
            org,
        )
        return membership, active_org_id, has_full_access, has_own_access

    def filter_queryset(self, queryset):
        """
        Enforce role-based visibility:
        - ADMIN/OWNER/VALIDATION_RESULTS_VIEWER: can see all runs in the active org.
        - Otherwise: only runs they launched in the active org.
        """

        user = self.request.user
        membership, active_org_id, has_full_access, has_own_access = (
            self._access_context()
        )
        if not membership or not active_org_id:
            return ValidationRun.objects.none()

        scoped = queryset
        if has_full_access:
            scoped = scoped.filter(org_id=active_org_id)
        elif has_own_access:
            scoped = scoped.filter(org_id=active_org_id, user_id=user.id)
        else:
            scoped = ValidationRun.objects.none()

        msg = (
            "ValidationRunViewSet.filter_queryset user=%s org=%s "
            "roles=%s full_access=%s "
            "filtered_ids=%s"
        )

        logger.debug(
            msg,
            user.id,
            active_org_id,
            membership.role_codes,
            has_full_access,
            list(scoped.values_list("id", flat=True)),
        )
        return super().filter_queryset(scoped)

    def get_queryset(self):
        if not (
            self.request and self.request.user and self.request.user.is_authenticated
        ):
            return ValidationRun.objects.none()

        user = self.request.user
        membership, active_org_id, has_full_access, has_own_access = (
            self._access_context()
        )
        if not membership or not active_org_id:
            return ValidationRun.objects.none()
        logger.debug(
            "ValidationRunViewSet.get_queryset user=%s org=%s roles=%s full_access=%s",
            user.id,
            active_org_id,
            membership.role_codes,
            has_full_access,
        )

        base_qs = (
            super()
            .get_queryset()
            .select_related(
                "workflow",
                "org",
                "submission",
            )
        )
        if has_full_access:
            qs = base_qs.filter(org_id=active_org_id)
        elif has_own_access:
            qs = base_qs.filter(org_id=active_org_id, user_id=user.id)
        else:
            qs = ValidationRun.objects.none()

        # Default recent-only (last 30 days) unless:
        # - ?all=1 provided, or
        # - any explicit date filter (after/before/on) provided.
        qp = self.request.query_params
        has_explicit_dates = any(k in qp for k in ("after", "before", "on"))
        if not truthy(qp.get("all")) and not has_explicit_dates:
            cutoff = timezone.now() - timedelta(days=30)
            qs = qs.filter(created__gte=cutoff)

        step_run_prefetch = Prefetch(
            "step_runs",
            queryset=ValidationStepRun.objects.select_related("workflow_step")
            .prefetch_related("findings", "findings__ruleset_assertion")
            .order_by("step_order", "pk"),
        )
        findings_prefetch = Prefetch(
            "findings",
            queryset=ValidationFinding.objects.select_related(
                "validation_step_run",
                "validation_step_run__workflow_step",
                "ruleset_assertion",
            ).order_by("severity", "-created"),
        )
        return qs.prefetch_related(step_run_prefetch, findings_prefetch)
