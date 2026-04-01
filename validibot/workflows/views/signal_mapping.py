"""Views for workflow-level signal mapping configuration.

Signal mappings define author-named signals (``s.name``) that extract
values from submission data paths.  These are resolved once before any
workflow step runs, making named values available in CEL expressions
across all steps.

The views follow the two-template modal CRUD pattern used by the
assertion editor:

- **Page view** (``WorkflowSignalMappingView``): renders the full HTML
  editor page with the mapping table, sample data card, and modal shell.
  Also returns just the table partial for HTMx-triggered refreshes.
- **Create/Edit modals** (``WorkflowSignalMappingCreateView``,
  ``WorkflowSignalMappingEditView``): GET returns the form partial
  inside the modal shell; POST validates and saves, returning a 204
  with ``HX-Trigger`` on success or a 200 with re-rendered form on
  validation error.
- **Delete** (``WorkflowSignalMappingDeleteView``): POST deletes the
  mapping and returns a 204 with ``signals-changed`` event.
- **Move** (``WorkflowSignalMappingMoveView``): POST swaps position
  values with the adjacent mapping.
- **Sample data** (``WorkflowSignalMappingSampleDataView``): POST
  parses pasted JSON/XML and returns candidate signals as an HTML
  partial (HTMx) or JSON (API backward compat).
"""

from __future__ import annotations

import json
import logging
import re
from http import HTTPStatus
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic.edit import FormView

from validibot.core.view_helpers import hx_trigger_response
from validibot.workflows.forms import WorkflowSignalMappingForm
from validibot.workflows.mixins import WorkflowObjectMixin
from validibot.workflows.models import WorkflowSignalMapping

logger = logging.getLogger(__name__)

MAX_FORMATTED_VALUE_LENGTH = 50


class WorkflowSignalMappingView(WorkflowObjectMixin, View):
    """GET: Render the signal mapping editor page.

    Serves the full HTML editor page with mapping table, sample data
    card, and modal shell.  When the request is an HTMx partial
    refresh (``HX-Request`` header present), returns only the table
    partial so the mapping list reloads in place.

    Also supports JSON responses (``Accept: application/json``) for
    backward compatibility with the existing test suite and any API
    consumers.

    Requires **manage** permission on the workflow.
    """

    def get(self, request, pk):
        workflow = self.get_workflow()
        if not self.user_can_manage_workflow():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        mappings = WorkflowSignalMapping.objects.filter(
            workflow=workflow,
        ).order_by("position")

        context = {
            "workflow": workflow,
            "mappings": mappings,
            "can_manage_workflow": self.user_can_manage_workflow(),
        }

        # HTMx partial refresh — return just the table partial
        if request.headers.get("HX-Request"):
            return render(
                request,
                "workflows/partials/signal_mapping_table.html",
                context,
            )

        # JSON API backward compat
        if "application/json" in request.headers.get("Accept", ""):
            return HttpResponse(
                json.dumps(
                    {
                        "workflow_id": workflow.pk,
                        "workflow_name": workflow.name,
                        "mappings": [
                            {
                                "id": m.pk,
                                "name": m.name,
                                "source_path": m.source_path,
                                "default_value": m.default_value,
                                "on_missing": m.on_missing,
                                "data_type": m.data_type,
                            }
                            for m in mappings
                        ],
                    }
                ),
                content_type="application/json",
            )

        return render(
            request,
            "workflows/workflow_signal_mapping.html",
            context,
        )


# ── Modal CRUD views ─────────────────────────────────────────────────
# These follow the assertion CRUD pattern: GET returns the modal
# content partial, POST validates/saves and returns hx_trigger_response.


class WorkflowSignalMappingCreateView(WorkflowObjectMixin, FormView):
    """Create a new signal mapping via modal form."""

    template_name = "workflows/partials/signal_mapping_form.html"
    form_class = WorkflowSignalMappingForm

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["workflow"] = self.get_workflow()
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        if self.request.GET.get("prefill_name"):
            initial["name"] = self.request.GET["prefill_name"]
        if self.request.GET.get("prefill_path"):
            initial["source_path"] = self.request.GET["prefill_path"]
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "modal_title": _("Add Signal"),
                "form_action": self.request.path,
                "submit_label": _("Add Signal"),
                "workflow": self.get_workflow(),
            }
        )
        return context

    def render_to_response(self, context, **response_kwargs):
        return render(
            self.request,
            self.template_name,
            context,
            status=response_kwargs.get("status", 200),
        )

    def form_valid(self, form):
        workflow = self.get_workflow()
        mapping = form.save_mapping(workflow)
        messages.success(self.request, _("Signal added."))
        return hx_trigger_response(
            message=_("Signal added."),
            close_modal="signalMappingModal",
            extra_payload={
                "signals-changed": {
                    "focus_mapping_id": mapping.pk,
                },
            },
            include_steps_changed=False,
        )


class WorkflowSignalMappingEditView(WorkflowObjectMixin, FormView):
    """Edit an existing signal mapping via modal form."""

    template_name = "workflows/partials/signal_mapping_form.html"
    form_class = WorkflowSignalMappingForm

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def _get_mapping(self) -> WorkflowSignalMapping:
        if not hasattr(self, "_mapping"):
            self._mapping = get_object_or_404(
                WorkflowSignalMapping,
                pk=self.kwargs.get("mapping_id"),
                workflow=self.get_workflow(),
            )
        return self._mapping

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["workflow"] = self.get_workflow()
        kwargs["exclude_mapping_id"] = self._get_mapping().pk
        return kwargs

    def get_initial(self):
        mapping = self._get_mapping()
        initial = {
            "name": mapping.name,
            "source_path": mapping.source_path,
            "on_missing": mapping.on_missing,
            "data_type": mapping.data_type,
        }
        if mapping.default_value is not None:
            initial["default_value"] = json.dumps(mapping.default_value)
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "modal_title": _("Edit Signal"),
                "form_action": self.request.path,
                "submit_label": _("Save Changes"),
                "workflow": self.get_workflow(),
            }
        )
        return context

    def render_to_response(self, context, **response_kwargs):
        return render(
            self.request,
            self.template_name,
            context,
            status=response_kwargs.get("status", 200),
        )

    def form_valid(self, form):
        mapping = self._get_mapping()
        form.save_mapping(self.get_workflow(), instance=mapping)
        messages.success(self.request, _("Signal updated."))
        return hx_trigger_response(
            message=_("Signal updated."),
            close_modal="signalMappingModal",
            extra_payload={
                "signals-changed": {
                    "focus_mapping_id": mapping.pk,
                },
            },
            include_steps_changed=False,
        )


class WorkflowSignalMappingDeleteView(WorkflowObjectMixin, View):
    """Delete a signal mapping."""

    def post(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        workflow = self.get_workflow()
        mapping = get_object_or_404(
            WorkflowSignalMapping,
            pk=self.kwargs.get("mapping_id"),
            workflow=workflow,
        )
        mapping.delete()
        messages.success(request, _("Signal removed."))
        return hx_trigger_response(
            message=_("Signal removed."),
            close_modal=None,
            extra_payload={"signals-changed": True},
            include_steps_changed=False,
        )


class WorkflowSignalMappingMoveView(WorkflowObjectMixin, View):
    """Move a signal mapping up or down in the display order."""

    def post(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        workflow = self.get_workflow()
        mapping = get_object_or_404(
            WorkflowSignalMapping,
            pk=self.kwargs.get("mapping_id"),
            workflow=workflow,
        )
        direction = request.POST.get("direction")
        mappings = list(
            WorkflowSignalMapping.objects.filter(
                workflow=workflow,
            ).order_by("position", "pk"),
        )
        try:
            index = mappings.index(mapping)
        except ValueError:
            return hx_trigger_response(
                status_code=400,
                message=_("Signal not found."),
                include_steps_changed=False,
            )

        if direction == "up" and index > 0:
            mappings[index - 1], mappings[index] = (
                mappings[index],
                mappings[index - 1],
            )
        elif direction == "down" and index < len(mappings) - 1:
            mappings[index], mappings[index + 1] = (
                mappings[index + 1],
                mappings[index],
            )
        else:
            return hx_trigger_response(
                status_code=204,
                close_modal=None,
                include_steps_changed=False,
            )

        with transaction.atomic():
            for pos, item in enumerate(mappings, start=1):
                WorkflowSignalMapping.objects.filter(pk=item.pk).update(
                    position=pos * 10,
                )

        return hx_trigger_response(
            close_modal=None,
            extra_payload={"signals-changed": True},
            include_steps_changed=False,
        )


# ── Sample data endpoint ─────────────────────────────────────────────
# Accepts pasted JSON/XML, traverses the structure, and returns
# candidate signal mappings.


class WorkflowSignalMappingSampleDataView(WorkflowObjectMixin, View):
    """POST: Parse sample JSON/XML data and return candidate signals.

    Accepts pasted sample data, traverses the structure to find all
    leaf values, and returns a list of candidate signal mappings with
    suggested names derived from the leaf key.

    Returns an HTML partial for HTMx requests, or JSON for API
    backward compatibility.

    Requires **manage** permission — this is a workflow authoring action.
    """

    def post(self, request, pk):
        workflow = self.get_workflow()
        if not self.user_can_manage_workflow():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)
        sample_text = request.POST.get("sample_data", "").strip()

        if not sample_text:
            return self._respond(
                request,
                workflow,
                error=_("No sample data provided."),
                status=400,
            )

        # Try JSON first, then XML
        try:
            data = json.loads(sample_text)
        except json.JSONDecodeError:
            try:
                from validibot.validations.xml_utils import xml_to_dict

                data = xml_to_dict(sample_text)
            except Exception:
                return self._respond(
                    request,
                    workflow,
                    error=_("Could not parse as JSON or XML."),
                    status=400,
                )

        # Get existing signal names to avoid duplicates
        existing_names = set(
            WorkflowSignalMapping.objects.filter(
                workflow=workflow,
            ).values_list("name", flat=True),
        )

        # Traverse and collect leaf paths
        candidates: list[dict[str, Any]] = []
        self._collect_leaves(data, "", candidates)

        # Deduplicate suggested names
        name_counts: dict[str, int] = {}
        for candidate in candidates:
            suggested = candidate["suggested_name"]
            if suggested in name_counts or suggested in existing_names:
                name_counts.setdefault(suggested, 1)
                name_counts[suggested] += 1
                candidate["suggested_name"] = f"{suggested}_{name_counts[suggested]}"
            else:
                name_counts[suggested] = 0

        return self._respond(request, workflow, candidates=candidates)

    def _respond(
        self,
        request,
        workflow,
        *,
        candidates: list[dict[str, Any]] | None = None,
        error: str | None = None,
        status: int = 200,
    ) -> HttpResponse:
        """Return HTML partial for HTMx, or JSON for API compat.

        HTMx requests always receive status 200, even for validation
        errors, because HTMx does not swap 4xx responses by default.
        The error is displayed inline via the rendered partial.  JSON
        API responses keep the original status code.
        """
        if request.headers.get("HX-Request"):
            return render(
                request,
                "workflows/partials/sample_data_results.html",
                {
                    "candidates": candidates or [],
                    "error": str(error) if error else None,
                    "workflow": workflow,
                },
            )
        # JSON fallback for API consumers and existing tests
        if error:
            return HttpResponse(
                json.dumps({"error": str(error)}),
                content_type="application/json",
                status=status,
            )
        return HttpResponse(
            json.dumps({"candidates": candidates or []}),
            content_type="application/json",
        )

    def _collect_leaves(
        self,
        data: Any,
        prefix: str,
        results: list[dict],
    ) -> None:
        """Recursively collect leaf values with their full paths."""
        if isinstance(data, dict):
            for key, value in data.items():
                path = f"{prefix}.{key}" if prefix else key
                if isinstance(value, (dict, list)):
                    self._collect_leaves(value, path, results)
                else:
                    suggested = self._sanitize_name(key, len(results))
                    results.append(
                        {
                            "path": path,
                            "value": self._format_value(value),
                            "suggested_name": suggested,
                        }
                    )
        elif isinstance(data, list):
            for i, item in enumerate(data):
                path = f"{prefix}[{i}]"
                if isinstance(item, (dict, list)):
                    self._collect_leaves(item, path, results)
                else:
                    results.append(
                        {
                            "path": path,
                            "value": self._format_value(item),
                            "suggested_name": f"item_{i}",
                        }
                    )

    @staticmethod
    def _sanitize_name(key: str, fallback_index: int) -> str:
        """Convert an arbitrary data key into a valid CEL identifier.

        Replaces spaces and hyphens with underscores, strips all other
        non-identifier characters, and prefixes with ``_`` if the result
        starts with a digit.  Falls back to ``field_<index>`` when
        nothing usable remains.
        """
        name = key.replace(" ", "_").replace("-", "_")
        name = re.sub(r"[^a-zA-Z0-9_]", "", name)
        if not name:
            return f"field_{fallback_index}"
        if name[0].isdigit():
            name = f"_{name}"
        return name

    @staticmethod
    def _format_value(value: Any) -> str:
        """Format a value for display (truncated if long)."""
        s = str(value)
        return (
            s[:MAX_FORMATTED_VALUE_LENGTH] + "..."
            if len(s) > MAX_FORMATTED_VALUE_LENGTH
            else s
        )


# ── Output promotion ─────────────────────────────────────────────────
# Toggle signal_name on a SignalDefinition to promote a validator
# output into the s.* namespace for downstream steps.


class WorkflowStepPromoteOutputView(WorkflowObjectMixin, View):
    """POST: Set or clear signal_name on a SignalDefinition.

    Promotes a validator output into the ``s.*`` namespace so
    downstream steps can reference it by name.  Sending an empty
    ``signal_name`` clears the promotion.

    Requires **manage** permission on the workflow.
    """

    def post(self, request, *args, **kwargs):
        from validibot.validations.models import SignalDefinition
        from validibot.validations.services.signal_resolution import (
            validate_signal_name,
        )
        from validibot.validations.services.signal_resolution import (
            validate_signal_name_unique,
        )

        if not self.user_can_manage_workflow():
            raise PermissionDenied
        workflow = self.get_workflow()
        signal_def = get_object_or_404(
            SignalDefinition,
            pk=self.kwargs.get("signal_id"),
            workflow_step__workflow=workflow,
            workflow_step_id=self.kwargs.get("step_id"),
        )

        new_name = request.POST.get("signal_name", "").strip()

        if new_name:
            # Validate the proposed signal name
            errors = validate_signal_name(new_name)
            if errors:
                return HttpResponse(
                    json.dumps({"errors": errors}),
                    content_type="application/json",
                    status=400,
                )
            unique_errors = validate_signal_name_unique(
                workflow_id=workflow.pk,
                name=new_name,
                exclude_signal_def_id=signal_def.pk,
            )
            if unique_errors:
                return HttpResponse(
                    json.dumps({"errors": unique_errors}),
                    content_type="application/json",
                    status=400,
                )

        signal_def.signal_name = new_name
        signal_def.save(update_fields=["signal_name"])

        if new_name:
            msg = _("Output promoted to s.%(name)s.") % {"name": new_name}
        else:
            msg = _("Output promotion removed.")
        messages.success(request, msg)
        return hx_trigger_response(
            message=msg,
            close_modal=None,
            extra_payload={"signals-changed": True},
            include_steps_changed=False,
        )
