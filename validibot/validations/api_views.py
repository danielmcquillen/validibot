"""
Org-scoped API viewsets for validation runs.

These viewsets implement the org-scoped routing pattern from ADR-2026-01-06:
    /api/v1/orgs/<org_slug>/runs/
    /api/v1/orgs/<org_slug>/runs/<pk>/
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import permissions
from rest_framework import viewsets

from validibot.core.api.org_scoped import OrgMembershipPermission
from validibot.core.api.org_scoped import OrgScopedMixin
from validibot.core.utils import truthy
from validibot.users.constants import PermissionCode
from validibot.validations.models import ValidationRun
from validibot.validations.serializers import ValidationRunSerializer
from validibot.validations.views import ValidationRunFilter

if TYPE_CHECKING:
    from django.db.models import QuerySet

logger = logging.getLogger(__name__)


class OrgScopedRunViewSet(OrgScopedMixin, viewsets.ReadOnlyModelViewSet):
    """
    Read-only API endpoints for validation runs within an organization.

    Provides:
    - list: List runs in the organization
    - retrieve: Get a run by ID

    URL patterns:
        GET /orgs/<org_slug>/runs/
        GET /orgs/<org_slug>/runs/<pk>/

    Access control:
    - Users with VALIDATION_RESULTS_VIEW_ALL can see all runs in the org
    - Users with VALIDATION_RESULTS_VIEW_OWN can see only their runs
    """

    serializer_class = ValidationRunSerializer
    permission_classes = [permissions.IsAuthenticated, OrgMembershipPermission]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = ValidationRunFilter
    ordering_fields = ["created", "id", "status"]
    ordering = ["-created", "-id"]
    http_method_names = ["get", "head", "options"]
    # Use default pk (id) lookup

    def get_queryset(self) -> QuerySet[ValidationRun]:
        """
        Return runs for the org, filtered by user permissions.

        - Full access: all runs in org
        - Own access: only runs created by the user
        - Default: only last 30 days unless ?all=1 or explicit date filter
        """
        user = self.request.user
        org = self.get_org()

        has_full_access = user.has_perm(
            PermissionCode.VALIDATION_RESULTS_VIEW_ALL.value,
            org,
        )
        has_own_access = user.has_perm(
            PermissionCode.VALIDATION_RESULTS_VIEW_OWN.value,
            org,
        )

        if has_full_access:
            qs = ValidationRun.objects.filter(org=org)
        elif has_own_access:
            qs = ValidationRun.objects.filter(org=org, user=user)
        else:
            return ValidationRun.objects.none()

        # Default recent-only (last 30 days) unless:
        # - ?all=1 provided, or
        # - any explicit date filter (after/before/on) provided.
        qp = self.request.query_params
        has_explicit_dates = any(k in qp for k in ("after", "before", "on"))
        if not truthy(qp.get("all")) and not has_explicit_dates:
            cutoff = timezone.now() - timedelta(days=30)
            qs = qs.filter(created__gte=cutoff)

        return qs
