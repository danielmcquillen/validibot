from __future__ import annotations

from django.contrib import messages
from django.contrib.messages.views import SuccessMessageMixin
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView
from django.views.generic import ListView
from django.views.generic import UpdateView

from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.core.utils import reverse_with_org
from simplevalidations.projects.forms import ProjectForm
from simplevalidations.projects.models import Project
from simplevalidations.users.mixins import OrganizationAdminRequiredMixin


class ProjectListView(OrganizationAdminRequiredMixin, BreadcrumbMixin, ListView):
    organization_context_attr = "organization"
    template_name = "projects/project_list.html"
    context_object_name = "projects"
    paginate_by = 25

    def get_queryset(self):
        return Project.objects.filter(org=self.organization).order_by("name")

    def get_breadcrumbs(self):
        return [{"name": _("Projects"), "url": ""}]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["organization"] = self.organization
        context["create_url"] = reverse_with_org(
            "projects:project-create",
            request=self.request,
        )
        return context


class ProjectCreateView(
    OrganizationAdminRequiredMixin,
    BreadcrumbMixin,
    SuccessMessageMixin,
    CreateView,
):
    organization_context_attr = "organization"
    model = Project
    form_class = ProjectForm
    template_name = "projects/project_form.html"
    success_message = _("Project created.")

    def form_valid(self, form):
        form.instance.org = self.organization
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_with_org("projects:project-list", request=self.request)

    def get_breadcrumbs(self):
        return [
            {
                "name": _("Projects"),
                "url": reverse_with_org("projects:project-list", request=self.request),
            },
            {"name": _("New"), "url": ""},
        ]


class ProjectUpdateView(
    OrganizationAdminRequiredMixin,
    BreadcrumbMixin,
    SuccessMessageMixin,
    UpdateView,
):
    organization_context_attr = "organization"
    organization_lookup_kwarg = None
    model = Project
    form_class = ProjectForm
    template_name = "projects/project_form.html"
    success_message = _("Project updated.")

    def get_queryset(self):
        return Project.objects.filter(org=self.organization)

    def get_organization(self):
        if hasattr(self, "_permission_project"):
            return self._permission_project.org
        project = get_object_or_404(
            Project.objects.select_related("org"),
            pk=self.kwargs["pk"],
        )
        self._permission_project = project
        return project.org

    def get_object(self, queryset=None):
        if hasattr(self, "_permission_project"):
            return self._permission_project
        return super().get_object(queryset)

    def get_success_url(self):
        return reverse_with_org("projects:project-list", request=self.request)

    def get_breadcrumbs(self):
        return [
            {
                "name": _("Projects"),
                "url": reverse_with_org("projects:project-list", request=self.request),
            },
            {"name": self.object.name, "url": ""},
        ]


class ProjectDeleteView(OrganizationAdminRequiredMixin, View):
    organization_context_attr = "organization"
    organization_lookup_kwarg = None

    def get_organization(self):
        if hasattr(self, "_project"):
            return self._project.org
        project = get_object_or_404(
            Project.all_objects.select_related("org"),
            pk=self.kwargs["pk"],
        )
        self._project = project
        return project.org

    def _handle(self, request, *args, **kwargs):
        project = getattr(self, "_project", None)
        if project is None:
            project = get_object_or_404(
                Project.all_objects.select_related("org"),
                pk=self.kwargs["pk"],
                org=self.organization,
            )
            self._project = project

        if not project.can_delete():
            messages.error(request, _("Default projects cannot be deleted."))
            return HttpResponseRedirect(
                reverse_with_org(
                    "projects:project-list",
                    request=request,
                ),
            )

        project.soft_delete()

        if request.headers.get("HX-Request"):
            response = HttpResponse("", status=200)
            response["HX-Trigger"] = "projectDeleted"
            return response

        messages.success(request, _("Project deleted."))
        return HttpResponseRedirect(
            reverse_with_org(
                "projects:project-list",
                request=request,
            ),
        )

    def post(self, request, *args, **kwargs):
        return self._handle(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        return self._handle(request, *args, **kwargs)
