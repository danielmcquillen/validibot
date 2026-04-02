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
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic.edit import FormView

from validibot.core.view_helpers import hx_trigger_response
from validibot.workflows.forms import WorkflowSignalMappingForm
from validibot.workflows.mixins import WorkflowObjectMixin
from validibot.workflows.models import WorkflowSignalMapping

logger = logging.getLogger(__name__)

MAX_FORMATTED_VALUE_LENGTH = 50
MAX_ENUM_PREVIEW_ITEMS = 5

# JSON Schema keywords used to detect whether pasted JSON is a schema
# rather than raw sample data.
_SCHEMA_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "const",
        "enum",
        "items",
        "properties",
        "$ref",
        "allOf",
        "anyOf",
        "oneOf",
        "if",
        "then",
        "else",
        "required",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "additionalProperties",
        "$defs",
        "definitions",
        "format",
    }
)

_MAX_SCHEMA_DEPTH = 20


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
            "page_help_slug": "signal-mapping",
            "breadcrumbs": [
                {
                    "name": _("Workflows"),
                    "url": reverse("workflows:workflow_list"),
                },
                {
                    "name": workflow.name,
                    "url": reverse(
                        "workflows:workflow_detail",
                        kwargs={"pk": workflow.pk},
                    ),
                },
                {"name": _("Signals"), "url": ""},
            ],
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


def _find_signal_references(workflow, signal_name: str) -> list[str]:
    """Find assertions in this workflow that reference a signal by name.

    Searches CEL expressions (``rhs["expr"]``), cached CEL previews
    (``cel_cache``), and guard conditions (``when_expression``) for
    the patterns ``s.<name>`` or ``signal.<name>``.

    Returns a list of human-readable descriptions of where the signal
    is referenced (e.g. "Step 1: My Step — assertion 'Check EUI'").
    """
    from validibot.validations.models import RulesetAssertion

    # Match s.name or signal.name as a whole word
    pattern = re.compile(
        rf"\b(?:s|signal)\.{re.escape(signal_name)}\b",
    )

    # Collect all step-level ruleset IDs for this workflow
    steps = workflow.steps.select_related("ruleset", "validator").all()
    ruleset_to_step: dict[int, Any] = {}
    for step in steps:
        if step.ruleset_id:
            ruleset_to_step[step.ruleset_id] = step
        if step.validator_id and hasattr(step.validator, "default_ruleset"):
            default_rs = step.validator.default_ruleset
            if default_rs:
                ruleset_to_step.setdefault(default_rs.pk, step)

    if not ruleset_to_step:
        return []

    assertions = RulesetAssertion.objects.filter(
        ruleset_id__in=ruleset_to_step.keys(),
    )

    references: list[str] = []
    for assertion in assertions:
        texts_to_check = []

        # CEL expression assertions store the expression in rhs["expr"]
        if isinstance(assertion.rhs, dict) and assertion.rhs.get("expr"):
            texts_to_check.append(assertion.rhs["expr"])

        # Basic assertions have a cached CEL preview
        if assertion.cel_cache:
            texts_to_check.append(assertion.cel_cache)

        # Guard conditions
        if assertion.when_expression:
            texts_to_check.append(assertion.when_expression)

        for text in texts_to_check:
            if pattern.search(text):
                step = ruleset_to_step.get(assertion.ruleset_id)
                step_label = (
                    f"Step {step.step_number}: {step.name}" if step else "Unknown step"
                )
                assertion_label = str(assertion)
                references.append(f'{step_label} — "{assertion_label}"')
                break  # One match per assertion is enough

    return references


class WorkflowSignalMappingDeleteView(WorkflowObjectMixin, View):
    """Delete a signal mapping.

    Before deleting, checks whether the signal is referenced in any
    CEL assertion within the workflow.  If it is, the delete is blocked
    and the user is told which assertions reference it.
    """

    def post(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        workflow = self.get_workflow()
        mapping = get_object_or_404(
            WorkflowSignalMapping,
            pk=self.kwargs.get("mapping_id"),
            workflow=workflow,
        )

        # Check for references in CEL assertions
        references = _find_signal_references(workflow, mapping.name)
        if references:
            ref_list = "; ".join(references)
            error_msg = _(
                "Cannot delete signal '%(name)s' — it is referenced in: "
                "%(refs)s. Remove the references first."
            ) % {"name": mapping.name, "refs": ref_list}
            return hx_trigger_response(
                message=str(error_msg),
                level="error",
                status_code=200,
                close_modal=None,
                extra_payload={"signals-changed": False},
                include_steps_changed=False,
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


class WorkflowSignalMappingBulkAddView(WorkflowObjectMixin, View):
    """POST: Bulk-create signal mappings from sample data candidates.

    Accepts a JSON array of ``{name, source_path}`` pairs (the checked
    rows from the sample data results table) and creates one
    ``WorkflowSignalMapping`` per entry using default values for
    ``on_missing`` ("error") and ``data_type`` ("").

    Each candidate is validated individually: names must be valid CEL
    identifiers, not reserved, and unique within the workflow.  Invalid
    candidates are collected and reported back.  Valid candidates are
    created in a single atomic transaction so the operation is
    all-or-nothing for the valid subset.

    Returns an HTMx response with ``signals-changed`` on success, or
    a JSON error payload describing which candidates failed.
    """

    def post(self, request, pk):
        workflow = self.get_workflow()
        if not self.user_can_manage_workflow():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        # Accept candidates as a JSON string in POST form data or
        # as the raw JSON request body (for API consumers).
        raw = request.POST.get("candidates", "") or request.body.decode()
        try:
            candidates = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return HttpResponse(
                json.dumps({"error": "Invalid JSON."}),
                content_type="application/json",
                status=HTTPStatus.BAD_REQUEST,
            )

        if not isinstance(candidates, list) or not candidates:
            return HttpResponse(
                json.dumps({"error": "Expected a non-empty JSON array."}),
                content_type="application/json",
                status=HTTPStatus.BAD_REQUEST,
            )

        from validibot.validations.services.signal_resolution import (
            validate_signal_name,
        )
        from validibot.validations.services.signal_resolution import (
            validate_signal_name_unique,
        )

        errors: list[dict[str, Any]] = []
        valid: list[dict[str, str]] = []
        seen_names: set[str] = set()

        for candidate in candidates:
            name = (candidate.get("name") or "").strip()
            source_path = (candidate.get("source_path") or "").strip()

            if not name or not source_path:
                errors.append(
                    {"name": name, "error": "Name and source_path are required."},
                )
                continue

            name_errors = validate_signal_name(name)
            if name_errors:
                errors.append({"name": name, "error": " ".join(name_errors)})
                continue

            if name in seen_names:
                errors.append(
                    {"name": name, "error": f"Duplicate name '{name}' in this batch."},
                )
                continue

            unique_errors = validate_signal_name_unique(
                workflow_id=workflow.pk,
                name=name,
            )
            if unique_errors:
                errors.append({"name": name, "error": " ".join(unique_errors)})
                continue

            seen_names.add(name)
            valid.append({"name": name, "source_path": source_path})

        if errors and not valid:
            return HttpResponse(
                json.dumps({"errors": errors, "created": 0}),
                content_type="application/json",
                status=HTTPStatus.BAD_REQUEST,
            )

        # Determine starting position
        last_position = (
            WorkflowSignalMapping.objects.filter(workflow=workflow)
            .order_by("-position")
            .values_list("position", flat=True)
            .first()
        ) or 0

        with transaction.atomic():
            for i, entry in enumerate(valid):
                WorkflowSignalMapping.objects.create(
                    workflow=workflow,
                    name=entry["name"],
                    source_path=entry["source_path"],
                    on_missing="error",
                    data_type="",
                    position=last_position + (i + 1) * 10,
                )

        if errors:
            # Partial success: some created, some failed
            response = HttpResponse(
                json.dumps(
                    {
                        "created": len(valid),
                        "errors": errors,
                    }
                ),
                content_type="application/json",
                status=HTTPStatus.OK,
            )
            response["HX-Trigger"] = json.dumps({"signals-changed": True})
            return response

        return hx_trigger_response(
            message=_("%(count)d signal(s) added.") % {"count": len(valid)},
            close_modal=None,
            extra_payload={"signals-changed": True},
            include_steps_changed=False,
        )


# ── Sample data endpoint ─────────────────────────────────────────────
# Accepts pasted JSON/XML, traverses the structure, and returns
# candidate signal mappings.


class WorkflowSignalMappingSampleDataView(WorkflowObjectMixin, View):
    """POST: Parse sample JSON/XML/Schema data and return candidate signals.

    Accepts pasted sample data (raw JSON, XML, or a JSON Schema),
    traverses the structure to find all leaf values or schema properties,
    and returns a list of candidate signal mappings with suggested names.

    When the pasted JSON looks like a JSON Schema (has ``$schema`` key or
    matches a heuristic), property paths are extracted from the schema
    structure instead of treating schema keywords as data values.

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

        # Get existing mappings to detect already-added candidates
        existing_mappings = WorkflowSignalMapping.objects.filter(
            workflow=workflow,
        )
        existing_paths = {m.source_path for m in existing_mappings}
        existing_names = {m.name for m in existing_mappings}

        # Traverse and collect leaf paths (or schema property paths)
        candidates: list[dict[str, Any]] = []
        from_schema = isinstance(data, dict) and self._looks_like_json_schema(data)
        if from_schema:
            self._collect_schema_paths(data, "", candidates)
        else:
            self._collect_leaves(data, "", candidates)

        # Mark already-added candidates and deduplicate suggested names.
        # Already-added candidates keep their original suggested name
        # (they show "Added" and can't be re-added, so uniqueness
        # doesn't matter for them).  Only new candidates participate
        # in dedup so they get usable, non-colliding names.
        name_counts: dict[str, int] = {}
        for candidate in candidates:
            if candidate["path"] in existing_paths:
                candidate["already_added"] = True
                # Skip dedup — name is display-only for added rows
                continue

            candidate["already_added"] = False
            suggested = candidate["suggested_name"]
            if suggested in name_counts or suggested in existing_names:
                name_counts.setdefault(suggested, 1)
                name_counts[suggested] += 1
                candidate["suggested_name"] = f"{suggested}_{name_counts[suggested]}"
            else:
                name_counts[suggested] = 0

        return self._respond(
            request,
            workflow,
            candidates=candidates,
            from_schema=from_schema,
        )

    def _respond(
        self,
        request,
        workflow,
        *,
        candidates: list[dict[str, Any]] | None = None,
        error: str | None = None,
        status: int = 200,
        from_schema: bool = False,
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
                    "from_schema": from_schema,
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
        """Recursively collect leaf values with their full paths.

        Arrays of scalar values (strings, numbers, booleans) are treated
        as a single signal using the array's key as the name. Arrays of
        objects are recursed into so their nested fields become candidates.
        """
        if isinstance(data, dict):
            for key, value in data.items():
                path = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict):
                    self._collect_leaves(value, path, results)
                elif isinstance(value, list):
                    self._collect_list(value, key, path, results)
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
            self._collect_list(data, prefix, prefix, results)

    def _collect_list(
        self,
        items: list,
        key: str,
        prefix: str,
        results: list[dict],
    ) -> None:
        """Handle a list during leaf collection.

        If the list contains only scalars, emit a single candidate for
        the whole array (e.g. ``tags`` -> ``["gadgets", "mini"]``).
        If it contains dicts/lists, recurse into each element.
        """
        has_complex = any(isinstance(item, (dict, list)) for item in items)
        if not has_complex:
            # Scalar array — one signal for the whole array
            suggested = self._sanitize_name(key, len(results))
            results.append(
                {
                    "path": prefix,
                    "value": self._format_value(items),
                    "suggested_name": suggested,
                }
            )
        else:
            # Array of objects/arrays — recurse into each element
            for i, item in enumerate(items):
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

    # ── JSON Schema support ─────────────────────────────────────────

    @staticmethod
    def _looks_like_json_schema(data: dict) -> bool:
        """Return True if *data* appears to be a JSON Schema definition.

        Detection uses two strategies:

        1. **Definitive**: the ``$schema`` key is present.
        2. **Heuristic**: the top-level object has ``type: "object"``
           and a ``properties`` dict where at least half the property
           values contain recognized JSON Schema keywords.
        """
        if "$schema" in data:
            return True
        if data.get("type") != "object" or not isinstance(
            data.get("properties"),
            dict,
        ):
            return False
        props = data["properties"]
        if not props:
            return False
        schema_like = sum(
            1
            for v in props.values()
            if isinstance(v, dict) and v.keys() & _SCHEMA_KEYWORDS
        )
        return schema_like >= len(props) / 2

    def _collect_schema_paths(
        self,
        schema: dict,
        prefix: str,
        results: list[dict],
        *,
        _visited: set[str] | None = None,
        _depth: int = 0,
    ) -> None:
        """Recursively extract signal candidate paths from a JSON Schema.

        Walks ``properties``, ``items``, ``allOf``/``anyOf``/``oneOf``,
        and ``if``/``then``/``else`` branches to build the same candidate
        dicts that ``_collect_leaves`` produces from raw data.

        The ``value`` field shows type information (e.g. ``"string"``,
        ``"number"``) instead of a sample value.

        A ``_visited`` set prevents duplicates when the same path is
        declared in multiple composition branches.  Recursion is capped
        at ``_MAX_SCHEMA_DEPTH`` to guard against pathological schemas.
        """
        if _depth > _MAX_SCHEMA_DEPTH:
            return
        if _visited is None:
            _visited = set()

        # Walk declared properties
        for key, prop_schema in schema.get("properties", {}).items():
            if not isinstance(prop_schema, dict):
                continue
            path = f"{prefix}.{key}" if prefix else key

            if self._is_schema_discriminator(key, prop_schema):
                continue

            prop_type = self._resolve_schema_type(prop_schema)

            if prop_type == "object":
                self._collect_schema_paths(
                    prop_schema,
                    path,
                    results,
                    _visited=_visited,
                    _depth=_depth + 1,
                )
            elif prop_type == "array":
                items_schema = prop_schema.get("items")
                if isinstance(items_schema, dict):
                    items_type = self._resolve_schema_type(items_schema)
                    if items_type == "object" or "properties" in items_schema:
                        self._collect_schema_paths(
                            items_schema,
                            f"{path}[0]",
                            results,
                            _visited=_visited,
                            _depth=_depth + 1,
                        )
                    # Array of scalars — one signal for the whole array
                    elif path not in _visited:
                        _visited.add(path)
                        results.append(
                            {
                                "path": path,
                                "value": self._schema_type_display(
                                    prop_schema,
                                ),
                                "suggested_name": self._sanitize_name(
                                    key,
                                    len(results),
                                ),
                            },
                        )
                # Array without items schema — treat as single signal
                elif path not in _visited:
                    _visited.add(path)
                    results.append(
                        {
                            "path": path,
                            "value": self._schema_type_display(prop_schema),
                            "suggested_name": self._sanitize_name(
                                key,
                                len(results),
                            ),
                        },
                    )
            # Leaf scalar property
            elif path not in _visited:
                _visited.add(path)
                results.append(
                    {
                        "path": path,
                        "value": self._schema_type_display(prop_schema),
                        "suggested_name": self._sanitize_name(
                            key,
                            len(results),
                        ),
                    },
                )

        # Walk schema composition keywords (allOf, anyOf, oneOf)
        for keyword in ("allOf", "anyOf", "oneOf"):
            for sub_schema in schema.get(keyword, []):
                if isinstance(sub_schema, dict):
                    self._collect_schema_paths(
                        sub_schema,
                        prefix,
                        results,
                        _visited=_visited,
                        _depth=_depth + 1,
                    )

        # Walk conditional branches — ``then`` and ``else`` contain
        # properties that apply when the ``if`` condition matches.
        # The ``if`` branch itself is typically a discriminator.
        for keyword in ("then", "else"):
            sub = schema.get(keyword)
            if isinstance(sub, dict):
                self._collect_schema_paths(
                    sub,
                    prefix,
                    results,
                    _visited=_visited,
                    _depth=_depth + 1,
                )

    @staticmethod
    def _is_schema_discriminator(key: str, prop_schema: dict) -> bool:
        """Return True if a schema property is a structural discriminator.

        Properties whose key starts with ``@`` (JSON-LD convention for
        metadata like ``@type``, ``@id``) and whose definition contains
        only ``const``, ``enum``, or a bare ``type`` are discriminators
        that don't carry data values worth mapping to signals.
        """
        if not key.startswith("@"):
            return False
        meaningful_keys = set(prop_schema.keys()) - {
            "description",
            "title",
        }
        return meaningful_keys <= {"const", "enum", "type"}

    @staticmethod
    def _resolve_schema_type(prop_schema: dict) -> str:
        """Return the effective type string for a schema property.

        Handles the case where ``type`` is an array
        (e.g. ``["string", "null"]``) by picking the first non-null type.
        """
        prop_type = prop_schema.get("type", "")
        if isinstance(prop_type, list):
            return next((t for t in prop_type if t != "null"), prop_type[0])
        return prop_type

    @staticmethod
    def _schema_type_display(prop_schema: dict) -> str:
        """Format schema type info for the value column display.

        Shows the type, or ``const``/``enum`` constraints when present.
        """
        if "const" in prop_schema:
            val = str(prop_schema["const"])
            if len(val) > MAX_FORMATTED_VALUE_LENGTH:
                val = val[:MAX_FORMATTED_VALUE_LENGTH] + "..."
            return f"const: {val}"
        if "enum" in prop_schema:
            enum_vals = prop_schema["enum"]
            enum_str = ", ".join(str(v) for v in enum_vals[:MAX_ENUM_PREVIEW_ITEMS])
            suffix = ", ..." if len(enum_vals) > MAX_ENUM_PREVIEW_ITEMS else ""
            return f"enum: [{enum_str}{suffix}]"

        prop_type = prop_schema.get("type", "any")
        if isinstance(prop_type, list):
            prop_type = next(
                (t for t in prop_type if t != "null"),
                prop_type[0],
            )
        if prop_type == "array":
            items = prop_schema.get("items", {})
            items_type = items.get("type", "any") if isinstance(items, dict) else "any"
            return f"array of {items_type}s"
        return str(prop_type)


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
