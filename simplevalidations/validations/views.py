from datetime import timedelta

import django_filters
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView
from django.views.generic import ListView
from django.views.generic.edit import DeleteView
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework import permissions
from rest_framework import viewsets

from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.core.utils import reverse_with_org
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.serializers import ValidationRunSerializer
from simplevalidations.workflows.models import Workflow


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
        org_ids = user.memberships.filter(is_active=True).values_list("org_id", flat=True)
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
                "url": reverse_with_org("validations:validation_list", request=self.request),
            },
        )
        breadcrumbs.append(
            {
                "name": _("Run #%(pk)s") % {"pk": validation.pk},
                "url": "",
            },
        )
        return breadcrumbs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        return context


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
                "url": reverse_with_org("validations:validation_list", request=self.request),
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
