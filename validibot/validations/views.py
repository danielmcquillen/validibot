import json
import logging
import re
from datetime import timedelta

import django_filters
from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.db import models
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView
from django.views.generic import ListView
from django.views.generic import TemplateView
from django.views.generic import View
from django.views.generic.edit import DeleteView
from django.views.generic.edit import FormView
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import permissions
from rest_framework import viewsets

from validibot.core.mixins import BreadcrumbMixin
from validibot.core.utils import reverse_with_org
from validibot.core.utils import truthy
from validibot.users.permissions import PermissionCode
from validibot.validations.constants import VALIDATION_LIBRARY_LAYOUT_SESSION_KEY
from validibot.validations.constants import VALIDATION_LIBRARY_TAB_SESSION_KEY
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import LibraryLayout
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.forms import CustomValidatorCreateForm
from validibot.validations.forms import CustomValidatorUpdateForm
from validibot.validations.forms import FMIValidatorCreateForm
from validibot.validations.forms import ValidatorCatalogEntryForm
from validibot.validations.forms import ValidatorRuleForm
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorCatalogEntry
from validibot.validations.models import ValidatorCatalogRule
from validibot.validations.models import ValidatorCatalogRuleEntry
from validibot.validations.models import default_supported_data_formats_for_validation
from validibot.validations.serializers import ValidationRunSerializer
from validibot.validations.services.fmi import FMIIntrospectionError
from validibot.validations.services.fmi import create_fmi_validator
from validibot.validations.services.fmi import run_fmu_probe
from validibot.validations.utils import create_custom_validator
from validibot.validations.utils import update_custom_validator
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


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


# UI Views
# ------------------------------------------------------------------------------


class ValidationRunAccessMixin(LoginRequiredMixin, BreadcrumbMixin):
    """Shared queryset helpers for validation run UI views."""

    allowed_sorts = {
        "created": "created",
        "-created": "-created",
        "status": "status",
        "-status": "-status",
        "workflow": "workflow__name",
        "-workflow": "-workflow__name",
    }

    def get_base_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return ValidationRun.objects.none()
        active_org = getattr(self.request, "active_org", None)
        active_org_id = (
            active_org.id if active_org else getattr(user, "current_org_id", None)
        )
        full_access_org_ids: set[int] = set()
        restricted_org_ids: set[int] = set()
        memberships = (
            user.memberships.filter(is_active=True)
            .select_related("org")
            .prefetch_related("roles")
        )
        for membership in memberships:
            if active_org_id and membership.org_id != active_org_id:
                continue
            org = membership.org
            if user.has_perm(
                PermissionCode.VALIDATION_RESULTS_VIEW_ALL.value,
                org,
            ):
                full_access_org_ids.add(membership.org_id)
            elif user.has_perm(
                PermissionCode.VALIDATION_RESULTS_VIEW_OWN.value,
                org,
            ):
                restricted_org_ids.add(membership.org_id)
        filters = models.Q()
        if full_access_org_ids:
            filters |= models.Q(org_id__in=full_access_org_ids)
        if restricted_org_ids:
            filters |= models.Q(org_id__in=restricted_org_ids, user_id=user.id)
        if not filters:
            return ValidationRun.objects.none()
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
        return (
            ValidationRun.objects.filter(filters)
            .select_related("workflow", "submission", "org")
            .prefetch_related(step_run_prefetch, findings_prefetch)
            .order_by("-created")
        )

    def get_ordering(self):
        sort = self.request.GET.get("sort", "-created")
        return self.allowed_sorts.get(sort, "-created")

    def get_queryset(self):
        return self.get_base_queryset()


class ValidationRunListView(ValidationRunAccessMixin, ListView):
    template_name = "validations/validation_list.html"
    context_object_name = "validations"
    paginate_by = 20
    page_size_options = (10, 50, 100)
    page_size_session_key = "validation_list_per_page"
    breadcrumbs = [
        {"name": _("Validations"), "url": ""},
    ]

    def get_queryset(self):
        qs = self.get_base_queryset()
        status_filter = self.request.GET.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        workflow_filter = self.request.GET.get("workflow")
        if workflow_filter:
            qs = qs.filter(workflow_id=workflow_filter)
        ordering = self.get_ordering()
        return qs.order_by(ordering)

    def get_paginate_by(self, queryset):
        per_page = self.request.GET.get("per_page")
        if per_page:
            try:
                per_page = int(per_page)
            except (TypeError, ValueError):
                per_page = None
            else:
                if per_page in self.page_size_options:
                    self.request.session[self.page_size_session_key] = per_page
                else:
                    per_page = None

        if per_page is None:
            per_page = self.request.session.get(self.page_size_session_key)

        if per_page not in self.page_size_options:
            per_page = self.paginate_by

        self.page_size = per_page
        return per_page

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        for validation in context.get("validations", []):
            can_view_all = user.has_perm(
                PermissionCode.VALIDATION_RESULTS_VIEW_ALL.value,
                validation,
            )
            can_view_own = user.has_perm(
                PermissionCode.VALIDATION_RESULTS_VIEW_OWN.value,
                validation,
            )
            validation.curr_user_can_view = bool(can_view_all or can_view_own)
            validation.curr_user_can_delete = user.has_perm(
                PermissionCode.ADMIN_MANAGE_ORG.value,
                validation,
            )
        context.update(
            {
                "current_sort": self.request.GET.get("sort", "-created"),
                "status_filter": self.request.GET.get("status", ""),
                "status_choices": ValidationRunStatus.choices,
                "workflow_options": Workflow.objects.for_user(self.request.user),
                "query_string": self._get_base_query_string(),
                "page_size_options": self.page_size_options,
                "current_page_size": getattr(self, "page_size", self.paginate_by),
            },
        )
        return context

    def _get_base_query_string(self):
        params = self.request.GET.copy()
        params.pop("page", None)
        return params.urlencode()


class ValidationRunDetailView(ValidationRunAccessMixin, DetailView):
    template_name = "validations/validation_detail.html"
    context_object_name = "run"

    def get_queryset(self):
        return self.get_base_queryset()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        run: ValidationRun = context["run"]
        step_runs = list(run.step_runs.all())
        findings = list(run.findings.all())
        context.update(
            {
                "step_runs": step_runs,
                "all_findings": findings,
                "summary_record": getattr(run, "summary_record", None),
            },
        )
        return context

    def get_breadcrumbs(self):
        validation = getattr(self, "object", None) or self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Validations"),
                "url": reverse_with_org(
                    "validations:validation_list", request=self.request
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": _("Run #%(pk)s") % {"pk": validation.pk},
                "url": "",
            },
        )
        return breadcrumbs


# Validator Library Views
# --------------------------------------------------------------------------


class ValidatorLibraryMixin(LoginRequiredMixin, BreadcrumbMixin):
    """Shared helpers for Validator Library views."""

    def get_active_org(self):
        org = getattr(self.request, "active_org", None)
        if org:
            return org
        if hasattr(self.request.user, "get_current_org"):
            return self.request.user.get_current_org()
        return None

    def get_active_membership(self):
        membership = getattr(self.request, "active_membership", None)
        if membership:
            return membership
        if hasattr(self.request.user, "membership_for_current_org"):
            return self.request.user.membership_for_current_org()
        return None

    def has_library_access(self) -> bool:
        org = self.get_active_org() or getattr(
            self.get_active_membership(), "org", None
        )
        if not org:
            return False
        return self.request.user.has_perm(
            PermissionCode.VALIDATOR_VIEW.value,
            org,
        )

    def can_manage_validators(self) -> bool:
        org = self.get_active_org() or getattr(
            self.get_active_membership(), "org", None
        )
        if not org:
            return False
        return self.request.user.has_perm(
            PermissionCode.VALIDATOR_EDIT.value,
            org,
        )

    def require_manage_permission(self):
        if not self.can_manage_validators():
            messages.error(
                self.request,
                _("You do not have permission to manage custom validators."),
            )
            return False
        if not self.get_active_org():
            messages.error(
                self.request,
                _("Select an organization before modifying validators."),
            )
            return False
        return True

    def require_library_access(self) -> bool:
        if not self.has_library_access():
            messages.error(
                self.request,
                _(
                    "Validator Library is limited to organization "
                    "owners, admins, and authors."
                ),
            )
            return False
        return True

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Validator Library"),
                "url": reverse_with_org(
                    "validations:validation_library",
                    request=self.request,
                ),
            },
        )
        return breadcrumbs

    def get_validator_queryset(self):
        """
        Return validators visible to the active org/user with rule and catalog
        relationships preloaded for display-only contexts.
        """
        org = self.get_active_org()
        queryset = (
            Validator.objects.select_related("custom_validator", "org")
            .prefetch_related(
                "catalog_entries",
                "rules",
                "rules__rule_entries",
                "rules__rule_entries__catalog_entry",
            )
            .order_by("validation_type", "name")
        )
        if org:
            queryset = queryset.filter(models.Q(is_system=True) | models.Q(org=org))
        else:
            queryset = queryset.filter(is_system=True)
        return queryset


class ValidationLibraryView(ValidatorLibraryMixin, TemplateView):
    template_name = "validations/library/library.html"
    default_tab = "custom"
    allowed_tabs = ("custom", "system")
    layout_param = "layout"
    default_layout = LibraryLayout.GRID
    allowed_layouts = set(LibraryLayout.values)
    layout_session_key = VALIDATION_LIBRARY_LAYOUT_SESSION_KEY

    def dispatch(self, request, *args, **kwargs):
        if not self.require_library_access():
            return redirect(
                reverse_with_org(
                    "workflows:workflow_list",
                    request=request,
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_breadcrumbs(self):
        return [
            {
                "name": _("Validator Library"),
                "url": "",
            },
        ]

    def get_active_tab(self):
        tab = (self.request.GET.get("tab") or "").lower()
        if tab in self.allowed_tabs:
            self._remember_tab(tab)
            return tab
        persisted = self.request.session.get(VALIDATION_LIBRARY_TAB_SESSION_KEY)
        if persisted in self.allowed_tabs:
            return persisted
        return self.default_tab

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.get_active_org()
        active_tab = self.get_active_tab()
        layout = str(self._get_layout())
        create_options = self._build_validator_create_options()
        default_selection = create_options[0]["value"] if create_options else None
        context.update(
            {
                "active_tab": active_tab,
                "current_layout": layout,
                "layout_urls": self._build_layout_urls(),
                "can_manage_validators": self.can_manage_validators(),
                "fmi_validator_create_url": reverse_with_org(
                    "validations:fmi_validator_create",
                    request=self.request,
                ),
                "validator_create_options": create_options,
                "validator_create_selected": default_selection,
                "system_validators": Validator.objects.filter(is_system=True)
                .exclude(release_state=ValidatorReleaseState.DRAFT)
                .order_by("order", "validation_type", "name")
                .select_related("custom_validator", "org"),
                "custom_validators": Validator.objects.filter(org=org)
                .order_by("name")
                .select_related("custom_validator"),
                "custom_validator_create_url": reverse_with_org(
                    "validations:custom_validator_create",
                    request=self.request,
                ),
                "custom_validators_empty_cta_modal_target": (
                    "#validatorCreateModal" if self.can_manage_validators() else None
                ),
                "custom_validators_empty_cta_url": (
                    reverse_with_org(
                        "validations:custom_validator_create",
                        request=self.request,
                    )
                    if self.can_manage_validators()
                    else None
                ),
                "custom_validators_empty_cta_label": _(
                    "Create the first custom validator"
                ),
            }
        )
        return context

    def _get_layout(self) -> str:
        requested = (self.request.GET.get(self.layout_param) or "").lower()
        if requested in self.allowed_layouts:
            self._remember_layout(requested)
            return requested
        persisted = self.request.session.get(self.layout_session_key)
        if persisted in self.allowed_layouts:
            return persisted
        return self.default_layout

    def _remember_layout(self, layout: str) -> None:
        try:
            self.request.session[self.layout_session_key] = layout
            self.request.session.modified = True
        except Exception:  # pragma: no cover - defensive
            return

    def _remember_tab(self, tab: str) -> None:
        try:
            self.request.session[VALIDATION_LIBRARY_TAB_SESSION_KEY] = tab
            self.request.session.modified = True
        except Exception:  # pragma: no cover - defensive
            return

    def _build_query_params(self, **overrides) -> str:
        params = self.request.GET.copy()
        for key, value in overrides.items():
            if value is None:
                params.pop(key, None)
            else:
                params[key] = value
        return params.urlencode()

    def _build_layout_urls(self) -> dict[str, str]:
        grid_query = self._build_query_params(layout=LibraryLayout.GRID)
        list_query = self._build_query_params(layout=LibraryLayout.LIST)
        return {
            "grid": f"?{grid_query}" if grid_query else "?",
            "list": f"?{list_query}" if list_query else "?",
        }

    def _build_validator_create_options(self) -> list[dict[str, str]]:
        return [
            {
                "value": "custom-basic",
                "name": str(_("Custom Basic Validator")),
                "subtitle": str(_("Author-defined")),
                "description": str(
                    _(
                        "Create a validator with custom inputs, "
                        "outputs, and assertions.",
                    ),
                ),
                "icon": "bi-sliders",
                "url": reverse_with_org(
                    "validations:custom_validator_create",
                    request=self.request,
                ),
            },
            {
                "value": "fmi",
                "name": str(_("FMI Validator")),
                "subtitle": str(_("Simulation-based")),
                "description": str(
                    _(
                        "Upload an FMU to auto-discover input and "
                        "output signals and create default assertions.",
                    ),
                ),
                "icon": "bi-cpu",
                "url": reverse_with_org(
                    "validations:fmi_validator_create",
                    request=self.request,
                ),
            },
        ]


class ValidatorDetailView(ValidatorLibraryMixin, DetailView):
    template_name = "validations/library/validator_detail.html"
    context_object_name = "validator"
    pk_url_kwarg = "pk"

    def get_object(self, queryset=None):
        qs = self.get_queryset()
        pk = self.kwargs.get("pk")
        slug_val = self.kwargs.get("slug")
        if pk:
            return get_object_or_404(qs, pk=pk)
        if slug_val:
            if str(slug_val).isdigit():
                return get_object_or_404(qs, pk=slug_val)
            return get_object_or_404(qs, slug=slug_val)
        return super().get_object(queryset)

    def dispatch(self, request, *args, **kwargs):
        if not self.require_library_access():
            return redirect(
                reverse_with_org(
                    "workflows:workflow_list",
                    request=request,
                ),
            )
        # Block access to non-published system validators (DRAFT or COMING_SOON)
        self.object = self.get_object()
        if self.object.is_system and not self.object.is_published:
            messages.warning(
                request,
                _("This validator is not yet available."),
            )
            return redirect(
                reverse_with_org(
                    "validations:validation_library",
                    request=request,
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return self.get_validator_queryset()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        validator = context["validator"]
        display = validator.catalog_display
        default_assertions = validator.rules.order_by("order", "name").prefetch_related(
            "rule_entries",
            "rule_entries__catalog_entry",
        )
        signal_choices = [
            (entry.id, f"{entry.slug}")
            for entry in validator.catalog_entries.order_by("slug").all()
        ]

        signal_create_form = ValidatorCatalogEntryForm(
            initial={"run_stage": CatalogRunStage.INPUT},
            validator=validator,
        )
        if not validator.has_processor:
            signal_create_form.fields["run_stage"].widget = forms.HiddenInput()
        context["signal_create_form"] = signal_create_form
        context["signal_edit_forms"] = {
            entry.id: {
                "form": ValidatorCatalogEntryForm(
                    instance=entry,
                    validator=validator,
                ),
                "title": _(
                    "Edit Input Signal"
                    if entry.run_stage == CatalogRunStage.INPUT
                    else "Edit Output Signal"
                ),
            }
            for entry in validator.catalog_entries.all()
        }
        context["default_assertion_create_form"] = ValidatorRuleForm(
            signal_choices=signal_choices,
        )
        context["default_assertion_edit_forms"] = {
            rule.id: ValidatorRuleForm(
                initial={
                    "name": rule.name,
                    "description": rule.description,
                    "rule_type": rule.rule_type,
                    "cel_expression": rule.expression,
                    "order": rule.order,
                    "signals": [
                        link.catalog_entry_id for link in rule.rule_entries.all()
                    ],
                },
                signal_choices=signal_choices,
            )
            for rule in default_assertions
        }
        show_output_tab = bool(validator.has_processor)
        requested_signals_tab = (
            self.request.GET.get("signals_tab") or "inputs"
        ).lower()
        allowed_signals_tabs = {"inputs"}
        if show_output_tab:
            allowed_signals_tabs.add("outputs")
        active_signals_tab = (
            requested_signals_tab
            if requested_signals_tab in allowed_signals_tabs
            else "inputs"
        )
        context.update(
            {
                "can_manage_validators": self.can_manage_validators(),
                "can_edit_validator": self.can_manage_validators()
                and not validator.is_system,
                "return_tab": self._resolve_return_tab(validator),
                "catalog_display": display,
                "catalog_entries": display.entries,
                "catalog_tab_prefix": "validator-detail",
                "validator_default_assertions": default_assertions,
                "show_output_tab": show_output_tab,
                "active_signals_tab": active_signals_tab,
                "probe_result": getattr(validator.fmu_model, "probe_result", None)
                if getattr(validator, "fmu_model", None)
                else None,
            },
        )
        return context

    def _resolve_return_tab(self, validator):
        remembered = self.request.session.get(VALIDATION_LIBRARY_TAB_SESSION_KEY)
        if remembered in {"system", "custom"}:
            return remembered
        requested = (self.request.GET.get("tab") or "").lower()
        if requested in {"system", "custom"}:
            return requested
        return "system" if validator.is_system else "custom"

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        validator = getattr(self, "object", None) or self.get_object()
        label = validator.name or validator.slug
        crumb_label = (
            _("View “%(name)s”") % {"name": label}
            if validator.is_system
            else _("Edit “%(name)s”") % {"name": label}
        )
        breadcrumbs.append(
            {
                "name": crumb_label,
                "url": "",
            },
        )
        return breadcrumbs


class ValidatorDefaultAssertionsView(ValidatorLibraryMixin, DetailView):
    """
    Render validator default assertions for display in a modal (HTMX target).
    """

    model = Validator
    context_object_name = "validator"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    template_name = (
        "validations/library/partials/validator_default_assertions_modal.html"
    )

    def dispatch(self, request, *args, **kwargs):
        if not self.require_library_access():
            return redirect(
                reverse_with_org(
                    "workflows:workflow_list",
                    request=request,
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return self.get_validator_queryset()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        validator = context["validator"]
        context.update(
            {
                "validator_default_assertions": validator.rules.order_by(
                    "order",
                    "name",
                ),
                "can_view_validator_detail": True,
            },
        )
        return context


class CustomValidatorManageMixin(ValidatorLibraryMixin):
    """Require author/admin access for CRUD operations."""

    def dispatch(self, request, *args, **kwargs):
        if not self.require_manage_permission():
            return redirect(
                reverse_with_org(
                    "validations:validation_library",
                    request=request,
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self, validator):
        return reverse_with_org(
            "validations:validator_detail",
            request=self.request,
            kwargs={"slug": validator.slug},
        )


class FMIValidatorCreateView(CustomValidatorManageMixin, FormView):
    template_name = "validations/library/fmi_validator_form.html"
    form_class = FMIValidatorCreateForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["org"] = self.get_active_org()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create FMI Validator")
        context["can_manage_validators"] = self.can_manage_validators()
        context["validator"] = None
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Create FMI validator"),
                "url": "",
            },
        )
        return breadcrumbs

    def form_valid(self, form):
        org = self.get_active_org()
        try:
            validator = create_fmi_validator(
                org=org,
                project=form.cleaned_data.get("project"),
                name=form.cleaned_data["name"],
                short_description=form.cleaned_data.get("short_description") or "",
                description=form.cleaned_data.get("description") or "",
                upload=form.cleaned_data["fmu_file"],
            )
        except FMIIntrospectionError as exc:
            form.add_error("fmu_file", str(exc))
            return self.form_invalid(form)
        messages.success(
            self.request,
            _("Created FMI validator “%(name)s”.") % {"name": validator.name},
        )
        return redirect(self.get_success_url(validator))


class FMIProbeStartView(CustomValidatorManageMixin, View):
    """HTMX endpoint to kick off an FMU probe inline."""

    def post(self, request, *args, **kwargs):
        validator = get_object_or_404(Validator, pk=kwargs["pk"])
        fmu = getattr(validator, "fmu_model", None)
        if not fmu:
            return JsonResponse(
                {"status": "error", "message": _("No FMU attached to this validator.")},
                status=400,
            )
        probe = getattr(fmu, "probe_result", None)
        if probe:
            probe.status = "PENDING"
            probe.last_error = ""
            probe.save(update_fields=["status", "last_error", "modified"])
        result = run_fmu_probe(fmu)
        # Refresh probe record to reflect latest status written by run_fmu_probe
        fmu.refresh_from_db(fields=["probe_result"])
        probe = getattr(fmu, "probe_result", None)
        payload = {
            "status": getattr(probe, "status", getattr(result, "status", "unknown")),
            "last_error": getattr(probe, "last_error", ""),
            "details": getattr(probe, "details", {}),
        }
        return JsonResponse(payload)


class FMIProbeStatusView(CustomValidatorManageMixin, View):
    """Return the latest probe status for polling."""

    def get(self, request, *args, **kwargs):
        validator = get_object_or_404(Validator, pk=kwargs["pk"])
        fmu = getattr(validator, "fmu_model", None)
        probe = getattr(fmu, "probe_result", None) if fmu else None
        if not probe:
            return JsonResponse(
                {"status": "missing", "message": _("Probe has not been requested.")},
                status=404,
            )
        data = {
            "status": probe.status,
            "last_error": probe.last_error,
            "details": probe.details,
        }
        return JsonResponse(data)


class CustomValidatorCreateView(CustomValidatorManageMixin, FormView):
    template_name = "validations/library/custom_validator_form.html"
    form_class = CustomValidatorCreateForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create Custom Basic Validator")
        context["can_manage_validators"] = self.can_manage_validators()
        context["validator"] = None
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Create new validator"),
                "url": "",
            },
        )
        return breadcrumbs

    def form_valid(self, form):
        org = self.get_active_org()
        custom_validator = create_custom_validator(
            org=org,
            user=self.request.user,
            name=form.cleaned_data["name"],
            short_description=form.cleaned_data.get("short_description") or "",
            description=form.cleaned_data.get("description") or "",
            custom_type=form.cleaned_data["custom_type"],
            notes=form.cleaned_data.get("notes") or "",
            version=form.cleaned_data.get("version") or "",
            allow_custom_assertion_targets=form.cleaned_data.get(
                "allow_custom_assertion_targets",
                False,
            ),
            supported_data_formats=[
                form.cleaned_data.get("supported_data_formats")
                or default_supported_data_formats_for_validation(
                    ValidationType.CUSTOM_VALIDATOR,
                )[0]
            ],
        )
        messages.success(
            self.request,
            _("Created custom validator “%(name)s”.")
            % {"name": custom_validator.validator.name},
        )
        return redirect(self.get_success_url(custom_validator.validator))


class CustomValidatorUpdateView(CustomValidatorManageMixin, FormView):
    template_name = "validations/library/custom_validator_form.html"
    form_class = CustomValidatorUpdateForm

    def dispatch(self, request, *args, **kwargs):
        try:
            self.custom_validator = self.get_object()
        except ObjectDoesNotExist:
            messages.error(
                request,
                _(
                    "This custom validator is missing its configuration. "
                    "Please recreate it from the Validator Library."
                ),
            )
            return redirect(
                reverse_with_org(
                    "validations:validation_library",
                    request=request,
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        org = self.get_active_org()
        validator = get_object_or_404(
            Validator,
            slug=self.kwargs["slug"],
            org=org,
            is_system=False,
        )
        return validator.custom_validator

    def get_initial(self):
        validator = self.custom_validator.validator
        return {
            "name": validator.name,
            "short_description": validator.short_description,
            "description": validator.description,
            "version": validator.version,
            "allow_custom_assertion_targets": validator.allow_custom_assertion_targets,
            "supported_data_formats": (
                validator.supported_data_formats[0]
                if validator.supported_data_formats
                else ""
            ),
            "notes": self.custom_validator.notes,
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        validator = self.custom_validator.validator
        context.update(
            {
                "form_title": _("Edit %(name)s Settings") % {"name": validator.name},
                "validator": validator,
                "can_manage_validators": self.can_manage_validators(),
            }
        )
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        validator = self.custom_validator.validator
        label = validator.name or validator.slug
        breadcrumbs.append(
            {
                "name": _("Edit “%(name)s”") % {"name": label},
                "url": reverse_with_org(
                    "validations:validator_detail",
                    request=self.request,
                    kwargs={"slug": validator.slug},
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": _("Edit Settings"),
                "url": "",
            },
        )
        return breadcrumbs

    def form_valid(self, form):
        custom = update_custom_validator(
            self.custom_validator,
            name=form.cleaned_data["name"],
            short_description=form.cleaned_data.get("short_description") or "",
            description=form.cleaned_data.get("description") or "",
            notes=form.cleaned_data.get("notes") or "",
            version=form.cleaned_data.get("version") or "",
            allow_custom_assertion_targets=form.cleaned_data.get(
                "allow_custom_assertion_targets",
            ),
            supported_data_formats=[
                form.cleaned_data.get("supported_data_formats")
                or default_supported_data_formats_for_validation(
                    ValidationType.CUSTOM_VALIDATOR,
                )[0]
            ],
        )
        messages.success(
            self.request,
            _("Updated custom validator “%(name)s”.") % {"name": custom.validator.name},
        )
        return redirect(self.get_success_url(custom.validator))


class CustomValidatorDeleteView(CustomValidatorManageMixin, TemplateView):
    template_name = "validations/library/custom_validator_confirm_delete.html"

    def dispatch(self, request, *args, **kwargs):
        self.custom_validator = self.get_object()
        return super().dispatch(request, *args, **kwargs)

    def get_object(self):
        org = self.get_active_org()
        validator = get_object_or_404(
            Validator,
            slug=self.kwargs["slug"],
            org=org,
            is_system=False,
        )
        return validator.custom_validator

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        validator = self.custom_validator.validator
        blockers = self._list_delete_blockers(validator)
        context.update(
            {
                "validator": validator,
                "can_manage_validators": self.can_manage_validators(),
                "delete_blockers": blockers,
                "can_delete": not blockers,
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        return self.handle_delete(request)

    def delete(self, request, *args, **kwargs):
        return self.handle_delete(request)

    def handle_delete(self, request):
        validator = self.custom_validator.validator
        blocker = self._get_delete_blocker(validator)
        if blocker:
            return self._delete_blocked_response(request, blocker)

        success_message = _("Deleted custom validator “%(name)s”.") % {
            "name": validator.name
        }
        validator.delete()
        if request.headers.get("HX-Request"):
            return self._hx_toast_response(success_message, status=200)
        messages.success(request, success_message)
        return redirect(
            reverse_with_org(
                "validations:validation_library",
                request=request,
            ),
        )

    def _get_delete_blocker(self, validator):
        if WorkflowStep.objects.filter(validator=validator).exists():
            return _(
                "Cannot delete %(name)s because workflow steps "
                "still reference this validator.",
            ) % {"name": validator.name}
        return None

    def _list_delete_blockers(self, validator):
        blockers: list[dict[str, str]] = []
        steps = WorkflowStep.objects.filter(validator=validator).select_related(
            "workflow",
        )
        for step in steps:
            workflow_name = step.workflow.name if step.workflow else _("Unknown")
            blockers.append(
                {
                    "label": _("Workflow step “%(step)s” (workflow: %(workflow)s)")
                    % {
                        "step": step.name,
                        "workflow": workflow_name,
                    },
                    "url": reverse_with_org(
                        "workflows:workflow_detail",
                        request=self.request,
                        kwargs={"pk": step.workflow_id},
                    )
                    if step.workflow_id
                    else "",
                }
            )
        return blockers

    def _delete_blocked_response(self, request, message):
        if request.headers.get("HX-Request"):
            return self._hx_toast_response(
                message,
                level="danger",
                status=400,
                reswap="none",
            )
        form = forms.Form(data={})
        form.full_clean()
        form.add_error(None, message)
        context = self.get_context_data()
        context["error_message"] = message
        context["form"] = form
        return render(
            request,
            self.template_name,
            context,
            status=200,
        )

    def _hx_toast_response(self, message, *, level="success", status=200, reswap=None):
        response = HttpResponse("", status=status)
        response["HX-Trigger"] = json.dumps(
            {
                "toast": {
                    "level": level,
                    "message": str(message),
                }
            }
        )
        if reswap:
            response["HX-Reswap"] = reswap
        return response


class ValidatorSignalMixin(CustomValidatorManageMixin):
    """Common helpers for validator signal CRUD."""

    validator: Validator

    def dispatch(self, request, *args, **kwargs):
        self.validator = get_object_or_404(
            Validator,
            pk=self.kwargs.get("pk"),
            is_system=False,
        )
        return super().dispatch(request, *args, **kwargs)

    def _hx_redirect(self):
        url = reverse_with_org(
            "validations:validator_detail",
            request=self.request,
            kwargs={"slug": self.validator.slug},
        )
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response

    def _redirect(self):
        return redirect(
            reverse_with_org(
                "validations:validator_detail",
                request=self.request,
                kwargs={"slug": self.validator.slug},
            ),
        )


class ValidatorSignalCreateView(ValidatorSignalMixin, FormView):
    form_class = ValidatorCatalogEntryForm

    def get(self, request, *args, **kwargs):
        """Handle GET requests to return fresh form content for HTMx modal."""
        stage = request.GET.get("run_stage") or CatalogRunStage.INPUT
        form = self.form_class(initial={"run_stage": stage}, validator=self.validator)
        if not self.validator.has_processor:
            form.fields["run_stage"].widget = forms.HiddenInput()

        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_signal_create.html",
                {
                    "validator": self.validator,
                    "modal_form": form,
                    "modal_id": "modal-signal-create",
                    "modal_title": _("Add Signal"),
                },
            )
        # Non-HTMx GET request - redirect to validator detail
        return self._redirect()

    def post(self, request, *args, **kwargs):
        stage = request.POST.get("run_stage") or CatalogRunStage.INPUT
        form = self.form_class(
            request.POST,
            initial={"run_stage": stage},
            validator=self.validator,
        )
        if not self.validator.has_processor:
            form.fields["run_stage"].widget = forms.HiddenInput()
        if form.is_valid():
            entry = form.save(commit=False)
            entry.validator = self.validator
            entry.save()
            messages.success(request, _("Signal created."))
            if request.headers.get("HX-Request"):
                return self._hx_redirect()
            return self._redirect()
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_signal_create.html",
                {
                    "validator": self.validator,
                    "modal_form": form,
                    "modal_id": "modal-signal-create",
                    "modal_title": _("Add Signal"),
                },
                status=200,
            )
        messages.error(request, _("Please correct the errors below."))
        return self._redirect()


class ValidatorSignalUpdateView(ValidatorSignalMixin, FormView):
    form_class = ValidatorCatalogEntryForm

    def post(self, request, *args, **kwargs):
        entry = get_object_or_404(
            ValidatorCatalogEntry,
            pk=self.kwargs.get("entry_pk"),
            validator=self.validator,
        )
        form = self.form_class(request.POST, instance=entry, validator=self.validator)
        if form.is_valid():
            form.save()
            messages.success(request, _("Signal updated."))
            if request.headers.get("HX-Request"):
                return self._hx_redirect()
            return self._redirect()
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_signal_edit.html",
                {
                    "validator": self.validator,
                    "entry_id": entry.id,
                    "form": form,
                },
                status=200,
            )
        messages.error(request, _("Please correct the errors below."))
        return self._redirect()


class ValidatorSignalDeleteView(ValidatorSignalMixin, TemplateView):
    def post(self, request, *args, **kwargs):
        entry = get_object_or_404(
            ValidatorCatalogEntry,
            pk=self.kwargs.get("entry_pk"),
            validator=self.validator,
        )
        try:
            entry.delete()
            messages.success(request, _("Signal deleted."))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        if request.headers.get("HX-Request"):
            return self._hx_redirect()
        return self._redirect()


class ValidatorSignalListView(ValidatorSignalMixin, TemplateView):
    """Legacy list route redirects to the validator detail page."""

    def get(self, request, *args, **kwargs):
        return redirect(
            reverse_with_org(
                "validations:validator_detail",
                request=request,
                kwargs={"pk": self.validator.pk},
            ),
        )


class ValidatorRuleMixin(CustomValidatorManageMixin):
    """Common helpers for validator default assertion CRUD."""

    validator: Validator

    def dispatch(self, request, *args, **kwargs):
        self.validator = get_object_or_404(
            Validator,
            pk=self.kwargs.get("pk"),
            is_system=False,
        )
        return super().dispatch(request, *args, **kwargs)

    def _hx_redirect(self):
        url = reverse_with_org(
            "validations:validator_detail",
            request=self.request,
            kwargs={"slug": self.validator.slug},
        )
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response

    def _can_move_rule(self) -> bool:
        membership = self.get_active_membership()
        if not membership:
            return False
        if self.request.user.has_perm(
            PermissionCode.ADMIN_MANAGE_ORG.value,
            self.validator,
        ):
            return True
        if not self.request.user.has_perm(
            PermissionCode.WORKFLOW_EDIT.value,
            self.validator,
        ):
            return False
        custom = getattr(self.validator, "custom_validator", None)
        return bool(custom and custom.created_by_id == self.request.user.id)

    def _redirect(self):
        return redirect(
            reverse_with_org(
                "validations:validator_detail",
                request=self.request,
                kwargs={"slug": self.validator.slug},
            ),
        )

    def _resolve_selected_entries(
        self, signals: list[str]
    ) -> list[ValidatorCatalogEntry]:
        ids = [int(pk) for pk in signals or [] if str(pk).isdigit()]
        return list(
            self.validator.catalog_entries.filter(pk__in=ids).order_by("slug"),
        )

    def _validate_cel_expression(
        self, expr: str, available_entries: list[ValidatorCatalogEntry]
    ) -> list[ValidatorCatalogEntry]:
        """Validate CEL and return the catalog entries that are referenced.

        The parser is intentionally lightweight: it confirms the expression
        references only known signals while allowing CEL literals, built-ins,
        and lambda variables. Output signals may be referenced as
        ``output.<slug>``.
        """
        expr = (expr or "").strip()
        if not expr:
            raise ValidationError(_("CEL expression is required."))
        if not self._delimiters_balanced(expr):
            raise ValidationError(_("Parentheses and brackets must be balanced."))

        reserved_literals = {"true", "false", "null", "payload", "output"}
        cel_builtins = {
            "has",
            "exists",
            "exists_one",
            "all",
            "map",
            "filter",
            "size",
            "contains",
            "startsWith",
            "endsWith",
            "type",
            "int",
            "double",
            "string",
            "bool",
            "abs",
            "ceil",
            "floor",
            "round",
            "timestamp",
            "duration",
            "matches",
            "in",
        }

        slug_map = {entry.slug: entry for entry in available_entries}
        referenced: set[ValidatorCatalogEntry] = set()
        unknown: set[str] = set()

        # Capture explicit output.<slug> references.
        for match in re.finditer(r"output\.([A-Za-z_][A-Za-z0-9_]*)", expr):
            slug = match.group(1)
            if slug in slug_map:
                referenced.add(slug_map[slug])
            else:
                unknown.add(f"output.{slug}")

        # Capture bare identifiers and allow literals/built-ins/lambda vars.
        for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", expr):
            name = match.group(0)
            if name in reserved_literals or name in cel_builtins:
                continue
            if name in slug_map:
                referenced.add(slug_map[name])
                continue
            # Treat single-character names as likely lambda variables.
            if len(name) == 1:
                continue
            unknown.add(name)

        if unknown:
            raise ValidationError(
                _("Unknown signal(s) referenced: %(names)s")
                % {"names": ", ".join(sorted(unknown))}
            )
        return list(referenced)

    @staticmethod
    def _delimiters_balanced(expression: str) -> bool:
        pairs = {"(": ")", "[": "]", "{": "}"}
        stack: list[str] = []
        for char in expression:
            if char in pairs:
                stack.append(pairs[char])
            elif char in pairs.values():
                if not stack or stack.pop() != char:
                    return False
        return not stack


class ValidatorRuleCreateView(ValidatorRuleMixin, FormView):
    form_class = ValidatorRuleForm

    def post(self, request, *args, **kwargs):
        form = self.form_class(
            request.POST,
            signal_choices=[
                (entry.id, entry.slug)
                for entry in self.validator.catalog_entries.order_by("slug")
            ],
        )
        if form.is_valid():
            signals = form.cleaned_data.get("signals") or []
            selected_entries = self._resolve_selected_entries(signals)
            available_entries = list(
                self.validator.catalog_entries.order_by("slug"),
            )
            referenced_entries = self._validate_cel_expression(
                form.cleaned_data["cel_expression"],
                available_entries,
            )
            entries = list({*selected_entries, *referenced_entries})
            rule = ValidatorCatalogRule.objects.create(
                validator=self.validator,
                name=form.cleaned_data["name"],
                description=form.cleaned_data.get("description") or "",
                rule_type=form.cleaned_data["rule_type"],
                expression=form.cleaned_data["cel_expression"],
                order=form.cleaned_data.get("order") or 0,
            )
            for entry in entries:
                ValidatorCatalogRuleEntry.objects.create(
                    rule=rule,
                    catalog_entry=entry,
                )
            messages.success(request, _("Default assertion created."))
            if request.headers.get("HX-Request"):
                return self._hx_redirect()
            return self._redirect()
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_rule_create.html",
                {
                    "validator": self.validator,
                    "default_assertion_create_form": form,
                },
                status=400,
            )
        messages.error(request, _("Please correct the errors below."))
        return self._redirect()


class ValidatorRuleUpdateView(ValidatorRuleMixin, FormView):
    form_class = ValidatorRuleForm

    def post(self, request, *args, **kwargs):
        rule = get_object_or_404(
            ValidatorCatalogRule,
            pk=self.kwargs.get("rule_pk"),
            validator=self.validator,
        )
        form = self.form_class(
            request.POST,
            signal_choices=[
                (entry.id, entry.slug)
                for entry in self.validator.catalog_entries.order_by("slug")
            ],
        )
        if form.is_valid():
            signals = form.cleaned_data.get("signals") or []
            selected_entries = self._resolve_selected_entries(signals)
            available_entries = list(
                self.validator.catalog_entries.order_by("slug"),
            )
            referenced_entries = self._validate_cel_expression(
                form.cleaned_data["cel_expression"],
                available_entries,
            )
            entries = list({*selected_entries, *referenced_entries})
            rule.name = form.cleaned_data["name"]
            rule.description = form.cleaned_data.get("description") or ""
            rule.rule_type = form.cleaned_data["rule_type"]
            rule.expression = form.cleaned_data["cel_expression"]
            rule.order = form.cleaned_data.get("order") or 0
            rule.save()
            rule.rule_entries.all().delete()
            for entry in entries:
                ValidatorCatalogRuleEntry.objects.create(
                    rule=rule,
                    catalog_entry=entry,
                )
            messages.success(request, _("Default assertion updated."))
            if request.headers.get("HX-Request"):
                return self._hx_redirect()
            return self._redirect()
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_rule_edit.html",
                {
                    "validator": self.validator,
                    "rule_id": rule.id,
                    "form": form,
                },
                status=400,
            )
        messages.error(request, _("Please correct the errors below."))
        return self._redirect()


class ValidatorRuleMoveView(ValidatorRuleMixin, View):
    """Move a default assertion up or down within a validator."""

    def post(self, request, *args, **kwargs):
        if not self._can_move_rule():
            return HttpResponse(status=403)
        direction = request.POST.get("direction")
        rule = get_object_or_404(
            ValidatorCatalogRule,
            pk=self.kwargs.get("rule_pk"),
            validator=self.validator,
        )
        rules = list(
            ValidatorCatalogRule.objects.filter(validator=self.validator).order_by(
                "order",
                "pk",
            )
        )
        try:
            index = rules.index(rule)
        except ValueError:
            return HttpResponse(status=404)

        if direction == "up" and index > 0:
            rules[index - 1], rules[index] = rules[index], rules[index - 1]
        elif direction == "down" and index < len(rules) - 1:
            rules[index], rules[index + 1] = rules[index + 1], rules[index]
        else:
            return HttpResponse(status=204)

        with transaction.atomic():
            for pos, item in enumerate(rules, start=1):
                ValidatorCatalogRule.objects.filter(pk=item.pk).update(order=pos * 10)

        assertions = (
            ValidatorCatalogRule.objects.filter(validator=self.validator)
            .order_by("order", "name")
            .prefetch_related(
                "rule_entries",
                "rule_entries__catalog_entry",
            )
        )
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/validator_default_assertions_card.html",
                {
                    "assertions": assertions,
                },
            )
        return redirect(
            reverse_with_org(
                "validations:validator_detail",
                request=request,
                kwargs={"slug": self.validator.slug},
            )
        )


class ValidatorRuleDeleteView(ValidatorRuleMixin, TemplateView):
    def post(self, request, *args, **kwargs):
        rule = get_object_or_404(
            ValidatorCatalogRule,
            pk=self.kwargs.get("rule_pk"),
            validator=self.validator,
        )
        rule.delete()
        messages.success(request, _("Default assertion deleted."))
        if request.headers.get("HX-Request"):
            return self._hx_redirect()
        return self._redirect()


class ValidatorRuleListView(ValidatorRuleMixin, TemplateView):
    """Legacy list route redirects to the validator detail page."""

    def get(self, request, *args, **kwargs):
        return redirect(
            reverse_with_org(
                "validations:validator_detail",
                request=request,
                kwargs={"pk": self.validator.pk},
            ),
        )


class ValidationRunDeleteView(ValidationRunAccessMixin, DeleteView):
    template_name = "validations/partials/validation_confirm_delete.html"

    def get_success_url(self):
        return reverse_with_org("validations:validation_list", request=self.request)

    def get_breadcrumbs(self):
        validation = getattr(self, "object", None) or self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Validations"),
                "url": reverse_with_org(
                    "validations:validation_list", request=self.request
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": _("Run #%(pk)s") % {"pk": validation.pk},
                "url": reverse_with_org(
                    "validations:validation_detail",
                    request=self.request,
                    kwargs={"pk": validation.pk},
                ),
            },
        )
        breadcrumbs.append({"name": _("Delete"), "url": ""})
        return breadcrumbs

    def post(self, request, *args, **kwargs):
        return self.delete(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        success_url = self.get_success_url()
        if not request.user.has_perm(
            PermissionCode.ADMIN_MANAGE_ORG.value,
            self.object,
        ):
            raise PermissionDenied(
                "You do not have permission to delete this validation run."
            )
        self.object.delete()
        messages.success(request, "Validation run removed.")
        if request.headers.get("HX-Request"):
            target = request.headers.get("HX-Target", "")
            response = HttpResponse("")
            response["HX-Trigger"] = "validationDeleted"
            if target.startswith("validation-row-"):
                return response
            response["HX-Redirect"] = success_url
            return response
        if request.method == "DELETE":
            return HttpResponse(status=204)
        return HttpResponseRedirect(success_url)


# Guest Validation Views
# ------------------------------------------------------------------------------


class GuestValidationRunListView(LoginRequiredMixin, ListView):
    """
    List validation runs for workflow guests.

    This view is for workflow guests (users with grants but no org memberships).
    It shows only the user's own validation runs across all workflows they have
    access to, with an organization filter.
    """

    template_name = "validations/guest_validation_list.html"
    context_object_name = "validations"
    paginate_by = 20
    page_size_options = (10, 50, 100)

    allowed_sorts = {
        "created": "created",
        "-created": "-created",
        "status": "status",
        "-status": "-status",
        "workflow": "workflow__name",
        "-workflow": "-workflow__name",
    }

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return ValidationRun.objects.none()

        # Get workflows the guest has access to via grants
        from validibot.workflows.models import Workflow

        accessible_workflows = Workflow.objects.for_user(user)

        # Only show the user's own runs on those workflows
        qs = ValidationRun.objects.filter(
            user=user,
            workflow__in=accessible_workflows,
        ).select_related("workflow", "workflow__org", "submission", "org")

        # Apply filters
        status_filter = self.request.GET.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        workflow_filter = self.request.GET.get("workflow")
        if workflow_filter:
            qs = qs.filter(workflow_id=workflow_filter)

        org_filter = self.request.GET.get("org")
        if org_filter:
            qs = qs.filter(org_id=org_filter)

        ordering = self._get_ordering()
        return qs.order_by(ordering)

    def _get_ordering(self):
        sort = self.request.GET.get("sort", "-created")
        return self.allowed_sorts.get(sort, "-created")

    def get_paginate_by(self, queryset):
        per_page = self.request.GET.get("per_page")
        if per_page:
            try:
                per_page = int(per_page)
            except (TypeError, ValueError):
                per_page = None
            else:
                if per_page not in self.page_size_options:
                    per_page = None
        if per_page is None:
            per_page = self.paginate_by
        self.page_size = per_page
        return per_page

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Mark all runs as viewable since these are the user's own runs
        for validation in context.get("validations", []):
            validation.curr_user_can_view = True
            validation.curr_user_can_delete = False  # Guests can't delete

        # Get org options for filter - orgs the user has workflow grants for
        from validibot.users.models import Organization
        from validibot.workflows.models import Workflow

        accessible_workflows = Workflow.objects.for_user(user)
        org_ids = accessible_workflows.values_list("org_id", flat=True).distinct()
        org_options = Organization.objects.filter(id__in=org_ids)

        context.update(
            {
                "current_sort": self.request.GET.get("sort", "-created"),
                "status_filter": self.request.GET.get("status", ""),
                "status_choices": ValidationRunStatus.choices,
                "workflow_options": accessible_workflows,
                "org_options": org_options,
                "org_filter": self.request.GET.get("org", ""),
                "query_string": self._get_base_query_string(),
                "page_size_options": self.page_size_options,
                "current_page_size": getattr(self, "page_size", self.paginate_by),
            },
        )
        return context

    def _get_base_query_string(self):
        params = self.request.GET.copy()
        params.pop("page", None)
        return params.urlencode()


class ValidatorSignalsListView(ValidatorLibraryMixin, DetailView):
    """Full-page list of all signals for a validator with complete details."""

    template_name = "validations/library/validator_signals_list.html"
    context_object_name = "validator"

    def get_object(self, queryset=None):
        qs = self.get_queryset()
        slug_val = self.kwargs.get("slug")
        if slug_val:
            if str(slug_val).isdigit():
                return get_object_or_404(qs, pk=slug_val)
            return get_object_or_404(qs, slug=slug_val)
        return super().get_object(queryset)

    def dispatch(self, request, *args, **kwargs):
        if not self.require_library_access():
            return redirect(
                reverse_with_org(
                    "workflows:workflow_list",
                    request=request,
                ),
            )
        # Block access to non-published system validators (DRAFT or COMING_SOON)
        self.object = self.get_object()
        if self.object.is_system and not self.object.is_published:
            messages.warning(
                request,
                _("This validator is not yet available."),
            )
            return redirect(
                reverse_with_org(
                    "validations:validation_library",
                    request=request,
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return self.get_validator_queryset()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        validator = context["validator"]
        # Get all signals ordered by stage then name
        signals = list(
            validator.catalog_entries.all().order_by("run_stage", "slug")
        )
        context.update(
            {
                "signals": signals,
                "can_manage_validators": self.can_manage_validators(),
            },
        )
        return context

    def get_breadcrumbs(self):
        validator = self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Validator Library"),
                "url": reverse_with_org(
                    "validations:validation_library",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": validator.name,
                "url": reverse_with_org(
                    "validations:validator_detail",
                    request=self.request,
                    kwargs={"slug": validator.slug},
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": _("Signals"),
                "url": "",
            },
        )
        return breadcrumbs


class CatalogEntryDetailView(LoginRequiredMixin, View):
    """Return modal content for a catalog entry detail view."""

    def get(self, request, entry_pk):
        entry = get_object_or_404(ValidatorCatalogEntry, pk=entry_pk)
        return render(
            request,
            "validations/library/partials/signal_detail_modal_content.html",
            {
                "signal": entry,
            },
        )
