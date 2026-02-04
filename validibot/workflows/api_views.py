"""
Org-scoped API viewsets for workflows.

These viewsets implement the org-scoped routing pattern from ADR-2026-01-06:
    /api/v1/orgs/<org_slug>/workflows/
    /api/v1/orgs/<org_slug>/workflows/<identifier>/
    /api/v1/orgs/<org_slug>/workflows/<slug>/versions/
    /api/v1/orgs/<org_slug>/workflows/<slug>/versions/<version>/
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from django.shortcuts import get_object_or_404
from rest_framework import permissions
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response as APIResponse

from validibot.core.api.org_scoped import OrgMembershipPermission
from validibot.core.api.org_scoped import OrgScopedMixin
from validibot.core.idempotency import idempotent
from validibot.validations.serializers import ValidationRunStartSerializer
from validibot.workflows.models import Workflow
from validibot.workflows.serializers import OrgScopedWorkflowSerializer
from validibot.workflows.version_utils import get_latest_workflow
from validibot.workflows.version_utils import get_latest_workflow_ids
from validibot.workflows.views_helpers import resolve_project
from validibot.workflows.views_launch_helpers import LaunchValidationError
from validibot.workflows.views_launch_helpers import build_submission_from_api
from validibot.workflows.views_launch_helpers import launch_api_validation_run

if TYPE_CHECKING:
    from django.db.models import QuerySet


class OrgScopedWorkflowViewSet(OrgScopedMixin, viewsets.ReadOnlyModelViewSet):
    """
    Browse and run workflows in an organization.

    **Identifiers:** Workflows are identified by their `slug`, which is unique
    within each organization. You can also use the numeric `id` if needed.

    **Versioning:** Workflows can have multiple versions. These endpoints always
    use the **latest version**. To pin a specific version, use the
    `/workflows/{slug}/versions/{version}/` endpoints instead.
    """

    throttle_scope: str | None = None
    serializer_class = OrgScopedWorkflowSerializer
    permission_classes = [permissions.IsAuthenticated, OrgMembershipPermission]
    # Allow lookup by slug (string) or pk (integer)
    lookup_value_regex = r"[^/]+"

    def get_queryset(self) -> QuerySet[Workflow]:
        """Return workflows for the org from the URL."""
        return Workflow.objects.filter(org=self.get_org(), is_archived=False)

    def get_object(self) -> Workflow:
        """
        Retrieve a workflow by slug (preferred) or numeric ID.

        Resolution order:
        1. Try slug lookup first
        2. If no match and identifier is numeric, try pk lookup
        3. If slug matches multiple versions, return the latest
        """
        queryset = self.filter_queryset(self.get_queryset())
        identifier = self.kwargs.get(self.lookup_field)

        # First, try slug-based lookup
        slug_matches = queryset.filter(slug=identifier)
        if slug_matches.exists():
            # Return the latest version if multiple exist
            latest = get_latest_workflow(slug_matches)
            if latest:
                self.check_object_permissions(self.request, latest)
                return latest

        # Second, if identifier is numeric, try pk lookup
        if identifier and identifier.isdigit():
            obj = get_object_or_404(queryset, pk=int(identifier))
            self.check_object_permissions(self.request, obj)
            return obj

        # No match found - get_object_or_404 will raise Http404
        return get_object_or_404(queryset, slug=identifier)

    def list(self, request, *args, **kwargs):
        """
        List all workflows in the organization.

        Returns only the **latest version** of each workflow. If a workflow
        has multiple versions, only the most recent one appears in this list.
        """
        queryset = self.filter_queryset(self.get_queryset())

        # Get IDs of latest versions only
        latest_ids = get_latest_workflow_ids(queryset)
        queryset = queryset.filter(pk__in=latest_ids)

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return APIResponse(serializer.data)

    def get_serializer_class(self):
        if getattr(self, "action", None) == "runs":
            return ValidationRunStartSerializer
        return super().get_serializer_class()

    def get_serializer_context(self):
        """Add org_slug to serializer context for URL generation."""
        context = super().get_serializer_context()
        context["org_slug"] = self.kwargs.get("org_slug")
        return context

    @action(
        detail=True,
        methods=["post"],
        url_path="runs",
        url_name="runs",
        throttle_scope="workflow_launch",
    )
    @idempotent
    def runs(self, request, *args, **kwargs):
        """
        Start a validation run.

        Submits a file for validation using this workflow. If the workflow has
        multiple versions, the latest version is used automatically.

        **Request format:** `multipart/form-data` with a `file` field, or JSON
        with base64-encoded `content`.

        **Response:** Returns the created run with its `id`. Poll the
        `/runs/{id}/` endpoint to check status and retrieve results.
        """
        workflow = self.get_object()
        project = resolve_project(workflow=workflow, request=request)
        try:
            submission_build = build_submission_from_api(
                request=request,
                workflow=workflow,
                user=request.user,
                project=project,
                serializer_factory=self.get_serializer,
                multipart_payload=lambda: request.data,
            )
        except LaunchValidationError as exc:
            status_code = exc.status_code
            if status_code == HTTPStatus.FORBIDDEN:
                status_code = HTTPStatus.NOT_FOUND
            return APIResponse(exc.payload, status=status_code)

        return launch_api_validation_run(
            request=request,
            workflow=workflow,
            submission_build=submission_build,
        )


class WorkflowVersionViewSet(OrgScopedMixin, viewsets.ReadOnlyModelViewSet):
    """
    Access specific versions of a workflow.

    Use these endpoints when you need to pin a particular workflow version
    for reproducibility, rather than always using the latest.
    """

    throttle_scope: str | None = None
    serializer_class = OrgScopedWorkflowSerializer
    permission_classes = [permissions.IsAuthenticated, OrgMembershipPermission]
    lookup_field = "version"
    lookup_value_regex = r"[^/]+"

    def get_queryset(self) -> QuerySet[Workflow]:
        """Return all versions of the workflow family."""
        workflow_slug = self.kwargs.get("workflow_slug")
        return Workflow.objects.filter(
            org=self.get_org(),
            slug=workflow_slug,
            is_archived=False,
        )

    def get_serializer_class(self):
        if getattr(self, "action", None) == "runs":
            return ValidationRunStartSerializer
        return super().get_serializer_class()

    def get_serializer_context(self):
        """Add org_slug to serializer context for URL generation."""
        context = super().get_serializer_context()
        context["org_slug"] = self.kwargs.get("org_slug")
        return context

    @action(
        detail=True,
        methods=["post"],
        url_path="runs",
        url_name="runs",
        throttle_scope="workflow_launch",
    )
    @idempotent
    def runs(self, request, *args, **kwargs):
        """
        Start a validation run using this specific version.

        Same as the main workflow runs endpoint, but uses this exact version
        instead of the latest.
        """
        workflow = self.get_object()
        project = resolve_project(workflow=workflow, request=request)
        try:
            submission_build = build_submission_from_api(
                request=request,
                workflow=workflow,
                user=request.user,
                project=project,
                serializer_factory=self.get_serializer,
                multipart_payload=lambda: request.data,
            )
        except LaunchValidationError as exc:
            status_code = exc.status_code
            if status_code == HTTPStatus.FORBIDDEN:
                status_code = HTTPStatus.NOT_FOUND
            return APIResponse(exc.payload, status=status_code)

        return launch_api_validation_run(
            request=request,
            workflow=workflow,
            submission_build=submission_build,
        )
