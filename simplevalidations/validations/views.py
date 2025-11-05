from datetime import timedelta
import json

import django_filters
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import models
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView
from django.views.generic import ListView
from django.views.generic import TemplateView
from django.views.generic.edit import DeleteView
from django.views.generic.edit import FormView
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import permissions
from rest_framework import viewsets

from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.core.utils import reverse_with_org
from simplevalidations.users.constants import RoleCode
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.forms import CustomValidatorCreateForm
from simplevalidations.validations.forms import CustomValidatorUpdateForm
from simplevalidations.validations.forms import RulesetAssertionForm
from simplevalidations.validations.models import CustomValidator
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.models import Validator
from simplevalidations.validations.serializers import ValidationRunSerializer
from simplevalidations.validations.utils import create_custom_validator
from simplevalidations.validations.utils import update_custom_validator
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.models import WorkflowStep


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
        if self.request and self.request.user:
            user = self.request.user
            # Only active memberships (avoid showing runs from inactive/disabled org links)
            org_ids = user.memberships.filter(is_active=True).values_list(
                "org_id",
                flat=True,
            )
            qs = super().get_queryset().filter(org__in=org_ids)

        else:
            qs = ValidationRun.objects.none()

        # Default recent-only (last 30 days) unless:
        # - ?all=1 provided, or
        # - any explicit date filter (after/before/on) provided.
        qp = self.request.query_params
        has_explicit_dates = any(k in qp for k in ("after", "before", "on"))
        if not _truthy(qp.get("all")) and not has_explicit_dates:
            cutoff = timezone.now() - timedelta(days=30)
            qs = qs.filter(created__gte=cutoff)

        return qs


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
        org_ids = user.memberships.filter(is_active=True).values_list(
            "org_id", flat=True
        )
        return (
            ValidationRun.objects.filter(org_id__in=org_ids)
            .select_related("workflow", "submission", "org")
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
    context_object_name = "validation"

    def get_queryset(self):
        return self.get_base_queryset()

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

    def can_manage_validators(self) -> bool:
        membership = self.get_active_membership()
        if not membership:
            return False
        return bool(
            membership.is_admin
            or membership.has_role(RoleCode.AUTHOR)
            or membership.has_role(RoleCode.OWNER)
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


class ValidationLibraryView(ValidatorLibraryMixin, TemplateView):
    template_name = "validations/library/library.html"
    default_tab = "custom"
    allowed_tabs = ("custom", "system")

    def get_breadcrumbs(self):
        return [
            {
                "name": _("Validator Library"),
                "url": "",
            },
        ]

    def get_active_tab(self):
        tab = (self.request.GET.get("tab") or self.default_tab).lower()
        if tab not in self.allowed_tabs:
            return self.default_tab
        return tab

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.get_active_org()
        active_tab = self.get_active_tab()
        context.update(
            {
                "active_tab": active_tab,
                "can_manage_validators": self.can_manage_validators(),
                "system_validators": Validator.objects.filter(is_system=True)
                .order_by("validation_type", "name")
                .select_related("custom_validator", "org"),
                "custom_validators": Validator.objects.filter(org=org)
                .order_by("name")
                .select_related("custom_validator"),
            }
        )
        return context


class ValidatorDetailView(ValidatorLibraryMixin, DetailView):
    template_name = "validations/library/validator_detail.html"
    context_object_name = "validator"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        org = self.get_active_org()
        qs = (
            Validator.objects.select_related("custom_validator", "org")
            .prefetch_related("catalog_entries")
            .order_by("validation_type", "name")
        )
        if org:
            qs = qs.filter(models.Q(is_system=True) | models.Q(org=org))
        else:
            qs = qs.filter(is_system=True)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_manage_validators"] = self.can_manage_validators()
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        validator = getattr(self, "object", None) or self.get_object()
        label = validator.name or validator.slug
        breadcrumbs.append({"name": label, "url": ""})
        return breadcrumbs


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


class CustomValidatorCreateView(CustomValidatorManageMixin, FormView):
    template_name = "validations/library/custom_validator_form.html"
    form_class = CustomValidatorCreateForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create Custom Validator")
        context["can_manage_validators"] = True
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
            description=form.cleaned_data.get("description") or "",
            custom_type=form.cleaned_data["custom_type"],
            notes=form.cleaned_data.get("notes") or "",
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

    def get_initial(self):
        validator = self.custom_validator.validator
        return {
            "name": validator.name,
            "description": validator.description,
            "notes": self.custom_validator.notes,
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        validator = self.custom_validator.validator
        context.update(
            {
                "form_title": _("Edit %(name)s") % {"name": validator.name},
                "validator": validator,
                "can_manage_validators": True,
            }
        )
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        validator = self.custom_validator.validator
        label = validator.name or validator.slug
        breadcrumbs.append(
            {
                "name": _("Edit %(name)s") % {"name": label},
                "url": "",
            },
        )
        return breadcrumbs

    def form_valid(self, form):
        custom = update_custom_validator(
            self.custom_validator,
            name=form.cleaned_data["name"],
            description=form.cleaned_data.get("description") or "",
            notes=form.cleaned_data.get("notes") or "",
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
        context.update(
            {
                "validator": self.custom_validator.validator,
                "can_manage_validators": True,
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
            return self._hx_toast_response(success_message, status=204)
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
                "Cannot delete %(name)s because workflow steps still reference this validator.",
            ) % {"name": validator.name}
        return None

    def _delete_blocked_response(self, request, message):
        if request.headers.get("HX-Request"):
            return self._hx_toast_response(
                message,
                level="danger",
                status=400,
                reswap="none",
            )
        messages.error(request, message)
        return redirect(
            reverse_with_org(
                "validations:validator_detail",
                request=request,
                kwargs={"slug": self.custom_validator.validator.slug},
            ),
        )

    def _hx_toast_response(self, message, *, level="success", status=200, reswap=None):
        response = HttpResponse(status=status)
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
