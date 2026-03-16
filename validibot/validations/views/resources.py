"""Resource file CRUD: create, update, and delete operations."""

import json
import logging

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from django.views.generic.edit import FormView

from validibot.core.filesafety import sanitize_filename
from validibot.core.utils import reverse_with_org
from validibot.validations.forms import ValidatorResourceFileForm
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.views.library import ValidatorLibraryMixin

logger = logging.getLogger(__name__)


class ResourceFileMixin(ValidatorLibraryMixin):
    """Common helpers for resource file CUD operations."""

    validator: Validator

    def dispatch(self, request, *args, **kwargs):
        if not self.can_manage_resource_files():
            messages.error(
                request,
                _("You do not have permission to manage resource files."),
            )
            return redirect(
                reverse_with_org(
                    "validations:validation_library",
                    request=request,
                ),
            )
        self.validator = get_object_or_404(
            Validator,
            pk=self.kwargs.get("pk"),
        )
        return super().dispatch(request, *args, **kwargs)

    def _hx_redirect_to_resource_files(self):
        url = reverse_with_org(
            "validations:validator_resource_files",
            request=self.request,
            kwargs={"slug": self.validator.slug},
        )
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response

    def _redirect_to_resource_files(self):
        return redirect(
            reverse_with_org(
                "validations:validator_resource_files",
                request=self.request,
                kwargs={"slug": self.validator.slug},
            ),
        )


class ResourceFileCreateView(ResourceFileMixin, FormView):
    """Create a new resource file via HTMX modal with file upload."""

    form_class = ValidatorResourceFileForm

    def get(self, request, *args, **kwargs):
        form = self.form_class(validator=self.validator)
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_resource_file_create.html",
                {
                    "validator": self.validator,
                    "modal_form": form,
                },
            )
        return self._redirect_to_resource_files()

    def post(self, request, *args, **kwargs):
        form = self.form_class(
            request.POST,
            request.FILES,
            validator=self.validator,
        )
        if form.is_valid():
            resource_file = form.save(commit=False)
            resource_file.validator = self.validator
            resource_file.org = self.get_active_org()
            resource_file.uploaded_by = request.user
            resource_file.filename = sanitize_filename(
                request.FILES["file"].name,
            )
            resource_file.save()
            messages.success(request, _("Resource file uploaded."))
            if request.headers.get("HX-Request"):
                return self._hx_redirect_to_resource_files()
            return self._redirect_to_resource_files()
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_resource_file_create.html",
                {
                    "validator": self.validator,
                    "modal_form": form,
                },
                status=200,
            )
        messages.error(request, _("Please correct the errors below."))
        return self._redirect_to_resource_files()


class ResourceFileUpdateView(ResourceFileMixin, FormView):
    """Edit resource file metadata (name, description, is_default)."""

    form_class = ValidatorResourceFileForm

    def post(self, request, *args, **kwargs):
        resource_file = get_object_or_404(
            ValidatorResourceFile,
            pk=self.kwargs.get("rf_pk"),
            validator=self.validator,
            org=self.get_active_org(),  # prevent editing system-wide files
        )
        form = self.form_class(
            request.POST,
            instance=resource_file,
            validator=self.validator,
            is_edit=True,
        )
        if form.is_valid():
            form.save()
            messages.success(request, _("Resource file updated."))
            if request.headers.get("HX-Request"):
                return self._hx_redirect_to_resource_files()
            return self._redirect_to_resource_files()
        if request.headers.get("HX-Request"):
            return render(
                request,
                "validations/library/partials/modal_resource_file_edit.html",
                {
                    "validator": self.validator,
                    "resource_file": resource_file,
                    "form": form,
                },
                status=200,
            )
        messages.error(request, _("Please correct the errors below."))
        return self._redirect_to_resource_files()


class ResourceFileDeleteView(ResourceFileMixin, TemplateView):
    """
    Delete a resource file with active-workflow blocker checks.

    Deletion is blocked if any active workflow step references this file
    via a ``WorkflowStepResource`` foreign-key relationship.
    """

    def post(self, request, *args, **kwargs):
        resource_file = get_object_or_404(
            ValidatorResourceFile,
            pk=self.kwargs.get("rf_pk"),
            validator=self.validator,
            org=self.get_active_org(),  # prevent deleting system-wide files
        )
        blockers = self._get_delete_blockers(resource_file)
        if blockers:
            blocker_names = ", ".join(blockers)
            message = _(
                "Cannot delete '%(name)s' because it is used by active "
                "workflow(s): %(workflows)s. Remove it from these workflows first."
            ) % {"name": resource_file.name, "workflows": blocker_names}
            if request.headers.get("HX-Request"):
                response = HttpResponse("", status=400)
                response["HX-Trigger"] = json.dumps(
                    {"toast": {"level": "danger", "message": str(message)}},
                )
                response["HX-Reswap"] = "none"
                return response
            messages.error(request, message)
            return self._redirect_to_resource_files()

        # Clean up WorkflowStepResource references from *inactive* workflows
        # before deleting.  The blocker check above already confirmed there are
        # no active-workflow references, but Django's PROTECT FK constraint
        # fires for all references regardless of workflow state.
        from validibot.workflows.models import WorkflowStepResource

        WorkflowStepResource.objects.filter(
            validator_resource_file=resource_file,
            step__workflow__is_active=False,
        ).delete()

        name = resource_file.name
        resource_file.delete()
        messages.success(
            request,
            _("Deleted resource file '%(name)s'.") % {"name": name},
        )
        if request.headers.get("HX-Request"):
            return self._hx_redirect_to_resource_files()
        return self._redirect_to_resource_files()

    def _get_delete_blockers(self, resource_file):
        """Return list of active workflow names that reference this resource file.

        Queries the ``WorkflowStepResource`` through table via the
        ``step_usages`` reverse relation (FK-backed, no JSON scanning).
        Only considers resources in active workflows.
        """
        from validibot.workflows.models import WorkflowStepResource

        usages = WorkflowStepResource.objects.filter(
            validator_resource_file=resource_file,
            step__workflow__is_active=True,
        ).select_related("step__workflow")

        return list({usage.step.workflow.name for usage in usages})
