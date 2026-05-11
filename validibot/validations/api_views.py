"""
Org-scoped API viewsets for validation runs.

These viewsets implement the org-scoped routing pattern:
    /api/v1/orgs/<org_slug>/runs/
    /api/v1/orgs/<org_slug>/runs/<pk>/
"""

from __future__ import annotations

import logging
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

from django.apps import apps
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Prefetch
from django.http import Http404
from django.http import HttpResponse
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import permissions
from rest_framework import viewsets
from rest_framework.decorators import action

from validibot.actions.constants import CredentialActionType
from validibot.core.api.org_scoped import OrgMembershipPermission
from validibot.core.api.org_scoped import OrgScopedMixin
from validibot.core.utils import truthy
from validibot.users.constants import PermissionCode
from validibot.validations.api.viewsets import ValidationRunFilter
from validibot.validations.credential_utils import (
    build_signed_credential_download_filename,
)
from validibot.validations.credential_utils import (
    extract_signed_credential_resource_label,
)
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.serializers import ValidationRunSerializer
from validibot.workflows.models import WorkflowStep

if TYPE_CHECKING:
    from django.db.models import QuerySet

logger = logging.getLogger(__name__)


class OrgScopedRunViewSet(OrgScopedMixin, viewsets.ReadOnlyModelViewSet):
    """
    View validation runs and their results.

    **Filtering:** By default, only runs from the last 30 days are returned.
    Use `?all=1` to retrieve all runs, or filter by date with `?after=`,
    `?before=`, or `?on=` parameters.

    **Permissions:** Depending on your role, you may see all runs in the
    organization or only runs you created.

    **Results:** Each run includes a `steps` array containing validation
    findings (issues) discovered during the run.
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
        elif self._user_has_guest_access_to_org(user, org):
            # Guests can poll/inspect runs they themselves launched
            # against workflows they currently have access to.
            #
            # Without the workflow narrowing, a per-workflow grant in
            # this org would expose the guest's runs for OTHER
            # workflows in the same org — including workflows whose
            # grants were revoked or workflows the guest never had a
            # grant for. The intersection with ``Workflow.objects.for_user``
            # delegates the per-workflow visibility decision to the
            # same helper the rest of the read-side uses, so a grant
            # revocation immediately closes the run-polling surface
            # too.
            #
            # ``OrgGuestAccess`` users see runs for every workflow in
            # the org because ``for_user`` returns the whole catalog
            # for them — that's the intended ALL-scope behaviour.
            from validibot.workflows.models import Workflow

            accessible_workflow_pks = (
                Workflow.objects.for_user(user)
                .filter(
                    org=org,
                )
                .values_list("pk", flat=True)
            )
            qs = ValidationRun.objects.filter(
                org=org,
                user=user,
                workflow__in=accessible_workflow_pks,
            )
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

        # Serializer-shape optimizations — see refactor-step item
        # ``[review-#5]``. The serializer reads ``workflow``, ``org``,
        # ``user``, and ``submission`` per row (FK lookups) and walks
        # ``step_runs → workflow_step → findings`` for each. Without
        # these joins + prefetches, an org with 10 k runs triggers
        # thousands of extra queries against the paginator.
        #
        # The ``_has_credential_action`` annotation pre-computes what
        # would otherwise be ``workflow.has_signed_credential_action``
        # (an ``EXISTS`` subquery) for every row. The serializer reads
        # this annotation in two places; without the annotation the
        # subquery runs N times per list. Pagination itself is already
        # applied globally via
        # ``REST_FRAMEWORK["DEFAULT_PAGINATION_CLASS"]`` (cursor-style,
        # resistant to offset-DoS), so no per-viewset attribute is
        # needed.
        return (
            qs.select_related(
                "workflow",
                "org",
                "user",
                "submission",
            )
            .prefetch_related(
                Prefetch(
                    "step_runs",
                    queryset=(
                        ValidationStepRun.objects.select_related(
                            "workflow_step__validator",
                        ).prefetch_related(
                            "findings",
                            # ``_build_signal_map`` and
                            # ``_build_template_param_meta`` iterate
                            # these to enrich output_signals /
                            # template_parameters_used. Without the
                            # prefetch, each step_run issues one
                            # query against signal_definitions
                            # (step-owned) plus one against the
                            # validator's signal_definitions —
                            # a classic N+1 on signal-bearing runs.
                            "workflow_step__signal_definitions",
                            "workflow_step__validator__signal_definitions",
                        )
                    ),
                ),
            )
            .annotate(
                _has_credential_action=Exists(
                    WorkflowStep.objects.filter(
                        workflow_id=OuterRef("workflow_id"),
                        action__definition__type=(
                            CredentialActionType.SIGNED_CREDENTIAL
                        ),
                    ),
                ),
            )
        )

    @staticmethod
    def _user_has_guest_access_to_org(user, org) -> bool:
        """True iff ``user`` has any active guest-style access to ``org``.

        Two qualifying paths:

        * Active ``WorkflowAccessGrant`` on any workflow in the org
          (per-workflow cross-org sharing).
        * Active ``OrgGuestAccess`` for the org (the ALL-scope guest
          invite acceptance path).

        Used to decide whether a non-member user can view the runs
        they themselves launched against this org. Without this
        carve-out, a successful guest launch would hand back a
        polling URL that 404s for the guest.
        """

        from validibot.workflows.models import OrgGuestAccess
        from validibot.workflows.models import WorkflowAccessGrant

        if WorkflowAccessGrant.objects.filter(
            user=user,
            workflow__org=org,
            is_active=True,
        ).exists():
            return True

        return OrgGuestAccess.objects.filter(
            user=user,
            org=org,
            is_active=True,
        ).exists()

    @action(
        detail=True,
        methods=["get"],
        url_path="credential/download",
        url_name="credential-download",
    )
    def credential_download(self, request, org_slug=None, pk=None):
        """Download the compact JWS credential for a validation run."""

        run = self.get_object()

        if apps.is_installed("validibot_pro"):
            from validibot_pro.credentials.models import IssuedCredential

            credential = IssuedCredential.objects.filter(workflow_run=run).first()
        else:
            credential = None

        if credential is None:
            raise Http404("No credential issued for this run.")

        resource_label = extract_signed_credential_resource_label(
            credential.payload_json,
        )
        download_name = build_signed_credential_download_filename(
            resource_label=resource_label,
            workflow_slug=run.workflow.slug if run.workflow else "",
            fallback_identifier=str(run.pk),
        )
        response = HttpResponse(
            credential.credential_jws,
            content_type="application/vc+jwt",
            status=HTTPStatus.OK,
        )
        response["Content-Disposition"] = f'attachment; filename="{download_name}"'
        return response
