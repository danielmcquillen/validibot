"""Workflow import/export views.

Import: a dedicated page with a drag-and-drop / file-select dropzone (HTMx,
indeterminate bar). The POST handles a ``.vaf`` or ``.json`` upload in one
request and always returns a *results* fragment (success, possibly with
warnings) or an *error* fragment — both swapped into the page, per the agreed
"always show results" flow. Export: a per-workflow download of the ``.vaf``
archive, linked from the workflow detail action bar.

Both are gated like workflow creation/management (``WORKFLOW_EDIT``) — importing
creates a workflow; exporting reveals its full definition.
"""

from __future__ import annotations

import logging

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.generic import TemplateView
from django.views.generic import View

from validibot.core.utils import reverse_with_org
from validibot.validations.validators.base.step_serializer import WorkflowImportError
from validibot.workflows.mixins import WorkflowAccessMixin
from validibot.workflows.mixins import WorkflowObjectMixin
from validibot.workflows.services.io.exporter import export_to_vaf
from validibot.workflows.services.io.importer import import_from_upload
from validibot.workflows.services.io.vaf import MAX_ARCHIVE_BYTES
from validibot.workflows.services.io.vaf import VafError

logger = logging.getLogger(__name__)


class WorkflowImportForm(forms.Form):
    """A single uploaded ``.vaf``/``.json`` workflow file."""

    file = forms.FileField(
        label=_("Workflow file"),
        widget=forms.ClearableFileInput(attrs={"accept": ".vaf,.json"}),
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > MAX_ARCHIVE_BYTES:
            limit_mb = MAX_ARCHIVE_BYTES // (1024 * 1024)
            raise forms.ValidationError(
                _("That file is larger than the %(limit)s MB import limit.")
                % {"limit": limit_mb},
            )
        return upload


class WorkflowImportView(WorkflowAccessMixin, TemplateView):
    """Render the import page and handle the upload POST.

    GET shows the dropzone; POST imports and swaps in a results/error fragment.
    """

    template_name = "workflows/import/workflow_import.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not self.user_can_create_workflow():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_breadcrumbs(self):
        return [
            {
                "name": _("Workflows"),
                "url": reverse_with_org(
                    "workflows:workflow_list", request=self.request
                ),
            },
            {"name": _("Import"), "url": ""},
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["import_url"] = reverse_with_org(
            "workflows:workflow_import",
            request=self.request,
        )
        context["list_url"] = reverse_with_org(
            "workflows:workflow_list",
            request=self.request,
        )
        return context

    def post(self, request, *args, **kwargs):
        form = WorkflowImportForm(request.POST, request.FILES)
        if not form.is_valid():
            message = " ".join(
                str(error) for errors in form.errors.values() for error in errors
            )
            return self._render_error(
                request,
                message=message or _("Please choose a workflow file to import."),
                code="vaf.no_file",
            )

        org = request.user.get_current_org()
        if org is None:
            return self._render_error(
                request,
                message=_("You need an organization before importing workflows."),
                code="vaf.no_org",
            )

        upload = form.cleaned_data["file"]
        try:
            result = import_from_upload(
                upload.read(),
                filename=upload.name,
                org=org,
                user=request.user,
            )
        except (VafError, WorkflowImportError) as exc:
            return self._render_error(
                request,
                message=str(exc),
                code=getattr(exc, "code", "vaf.import_failed"),
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Unexpected error importing a workflow")
            return self._render_error(
                request,
                message=_(
                    "Something went wrong importing this workflow. The file may "
                    "be corrupt or from an incompatible version.",
                ),
                code="vaf.unexpected",
            )

        # A toast for when the user later returns to the list (the results page
        # is the primary success surface).
        messages.success(
            request,
            _("Workflow “%(name)s” imported.") % {"name": result.workflow.name},
        )
        return self._render_results(request, result)

    # ── fragment renderers (swapped into #import-main) ──

    def _render_results(self, request, result):
        return render(
            request,
            "workflows/import/partials/import_results.html",
            {
                "workflow": result.workflow,
                "warnings": result.warnings,
                "components": result.components,
                "detail_url": reverse_with_org(
                    "workflows:workflow_detail",
                    request=request,
                    kwargs={"pk": result.workflow.pk},
                ),
                "list_url": reverse_with_org(
                    "workflows:workflow_list",
                    request=request,
                ),
            },
        )

    def _render_error(self, request, *, message, code):
        return render(
            request,
            "workflows/import/partials/import_error.html",
            {
                "error_message": message,
                "error_code": code,
                "import_url": reverse_with_org(
                    "workflows:workflow_import",
                    request=request,
                ),
                "list_url": reverse_with_org(
                    "workflows:workflow_list",
                    request=request,
                ),
            },
            # 200 so HTMx swaps the fragment in rather than treating it as a
            # transport error; the error is a normal, expected outcome.
            status=200,
        )


class WorkflowExportView(WorkflowObjectMixin, View):
    """Download a workflow as a ``.vaf`` archive."""

    def dispatch(self, request, *args, **kwargs):
        # Object-level check: export reveals the workflow's full definition, so
        # the user must be able to *edit this specific workflow*, not merely
        # manage workflows in their own current org. WorkflowObjectMixin resolves
        # the workflow through guest/public/cross-org access, so a current-org
        # manage check would let an author export a viewable workflow from
        # another org by guessing its pk. ``can_edit`` checks WORKFLOW_EDIT
        # against the resolved workflow's org.
        if request.user.is_authenticated and not self.get_workflow().can_edit(
            user=request.user,
        ):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        from validibot.core.templatetags.core_tags import app_version

        workflow = self.get_workflow()
        data = export_to_vaf(
            workflow,
            exported_by=str(getattr(request.user, "email", "") or request.user),
            exported_at=timezone.now().isoformat(),
            app_version=app_version(),
        )
        response = HttpResponse(data, content_type="application/octet-stream")
        response["Content-Disposition"] = (
            f'attachment; filename="{workflow.slug or "workflow"}.vaf"'
        )
        return response
