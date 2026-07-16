"""Validator library browsing: listing, detail, step I/O, and assertions."""

import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import models
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView
from django.views.generic import TemplateView
from django.views.generic import View

from validibot.core.mixins import BreadcrumbMixin
from validibot.core.utils import reverse_with_org
from validibot.users.permissions import PermissionCode
from validibot.validations.constants import VALIDATION_LIBRARY_LAYOUT_SESSION_KEY
from validibot.validations.constants import VALIDATION_LIBRARY_TAB_SESSION_KEY
from validibot.validations.constants import LibraryLayout
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.forms import StepIODefinitionForm
from validibot.validations.forms import ValidatorResourceFileForm
from validibot.validations.forms import ValidatorRuleForm
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import StepIODefinition
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorResourceFile

logger = logging.getLogger(__name__)


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

    def can_manage_resource_files(self) -> bool:
        """
        Check if current user can create/edit/delete resource files.

        Resource files consume storage and are shared org resources, so CUD
        operations are restricted to ADMIN/OWNER (ADMIN_MANAGE_ORG permission).
        """
        org = self.get_active_org() or getattr(
            self.get_active_membership(), "org", None
        )
        if not org:
            return False
        return self.request.user.has_perm(
            PermissionCode.ADMIN_MANAGE_ORG.value,
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
            Validator.objects.filter(
                availability_state=ValidatorAvailabilityState.AVAILABLE,
                is_enabled=True,
            )
            .select_related("custom_validator", "org")
            .prefetch_related(
                "step_io_definitions",
                "default_ruleset",
                "default_ruleset__assertions",
            )
            .order_by("validation_type", "name")
        )
        if org:
            queryset = queryset.filter(models.Q(is_system=True) | models.Q(org=org))
        else:
            queryset = queryset.filter(is_system=True)
        return queryset

    def get_object(self, queryset=None):
        """Resolve validator detail objects by pk, latest slug, or exact version.

        The default ``/library/custom/<slug>/`` route addresses a validator
        family and returns the latest visible row for that slug. Hidden manual
        routes under ``/versions/<int:version>/`` address an exact historical
        row. Workflow steps still reference a concrete validator FK; this is
        only library browsing behavior.
        """
        return self.get_library_validator_object(queryset)

    def get_library_validator_object(self, queryset=None):
        qs = queryset if queryset is not None else self.get_validator_queryset()
        pk = self.kwargs.get("pk")
        slug_val = self.kwargs.get("slug")
        version = self.kwargs.get("version")

        if pk:
            return get_object_or_404(qs, pk=pk)
        if slug_val:
            if str(slug_val).isdigit() and version is None:
                return get_object_or_404(qs, pk=slug_val)
            family_qs = qs.filter(slug=slug_val)
            if version is not None:
                return get_object_or_404(family_qs, version=version)
            family_qs = family_qs.exclude(
                is_system=True,
                release_state=ValidatorReleaseState.DRAFT,
            )
            latest = family_qs.order_by("-version", "-pk").first()
            if latest is None:
                raise Http404("No Validator matches the given query.")
            return latest
        raise Http404("No Validator matches the given query.")

    def is_version_specific_request(self) -> bool:
        return "version" in self.kwargs

    def is_latest_validator_version(self, validator: Validator) -> bool:
        latest = (
            self.get_validator_queryset()
            .filter(slug=validator.slug)
            .exclude(is_system=True, release_state=ValidatorReleaseState.DRAFT)
            .order_by("-version", "-pk")
            .first()
        )
        return latest is not None and latest.pk == validator.pk

    def latest_validators_by_slug(self, queryset):
        """Collapse a queryset to one latest row per slug."""
        latest_by_slug: dict[str, Validator] = {}
        for validator in queryset.order_by("slug", "-version", "-pk"):
            latest_by_slug.setdefault(validator.slug, validator)
        return sorted(
            latest_by_slug.values(),
            key=lambda validator: (
                validator.order,
                validator.validation_type,
                validator.name.lower(),
                validator.slug,
            ),
        )

    def build_validator_detail_urls(self, validator: Validator) -> dict[str, str]:
        """Return tab/detail URLs, preserving exact-version context when present."""
        if self.is_version_specific_request():
            kwargs = {"slug": validator.slug, "version": validator.version}
            return {
                "description": reverse_with_org(
                    "validations:validator_version_detail",
                    request=self.request,
                    kwargs=kwargs,
                ),
                "step_io": reverse_with_org(
                    "validations:validator_version_step_io_tab",
                    request=self.request,
                    kwargs=kwargs,
                ),
                "assertions": reverse_with_org(
                    "validations:validator_version_assertions_tab",
                    request=self.request,
                    kwargs=kwargs,
                ),
                "resource_files": reverse_with_org(
                    "validations:validator_version_resource_files",
                    request=self.request,
                    kwargs=kwargs,
                ),
                "step_io_list": reverse_with_org(
                    "validations:validator_version_step_io_list",
                    request=self.request,
                    kwargs=kwargs,
                ),
            }

        kwargs = {"slug": validator.slug}
        return {
            "description": reverse_with_org(
                "validations:validator_detail",
                request=self.request,
                kwargs=kwargs,
            ),
            "step_io": reverse_with_org(
                "validations:validator_step_io_tab",
                request=self.request,
                kwargs=kwargs,
            ),
            "assertions": reverse_with_org(
                "validations:validator_assertions_tab",
                request=self.request,
                kwargs=kwargs,
            ),
            "resource_files": reverse_with_org(
                "validations:validator_resource_files",
                request=self.request,
                kwargs=kwargs,
            ),
            "step_io_list": reverse_with_org(
                "validations:validator_step_io_list",
                request=self.request,
                kwargs=kwargs,
            ),
        }

    def add_validator_version_context(
        self,
        context: dict,
        validator: Validator,
    ) -> None:
        is_latest = self.is_latest_validator_version(validator)
        context.update(
            {
                "validator_detail_urls": self.build_validator_detail_urls(validator),
                "is_latest_validator_version": is_latest,
                "is_locked_validator_version": not is_latest,
                "validator_versions_url": reverse_with_org(
                    "validations:validator_versions",
                    request=self.request,
                    kwargs={"slug": validator.slug},
                ),
            },
        )


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
                "fmu_validator_create_url": reverse_with_org(
                    "validations:fmu_validator_create",
                    request=self.request,
                ),
                "validator_create_options": create_options,
                "validator_create_selected": default_selection,
                "system_validators": Validator.objects.filter(
                    availability_state=ValidatorAvailabilityState.AVAILABLE,
                    is_system=True,
                    is_enabled=True,
                )
                .exclude(release_state=ValidatorReleaseState.DRAFT)
                .select_related("custom_validator", "org"),
                "custom_validators": self.latest_validators_by_slug(
                    Validator.objects.filter(org=org).select_related(
                        "custom_validator",
                    ),
                ),
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
        context["system_validators"] = self.latest_validators_by_slug(
            context["system_validators"],
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
                "value": "fmu",
                "name": str(_("FMU Validator")),
                "subtitle": str(_("Simulation-based")),
                "description": str(
                    _(
                        "Upload an FMU to auto-discover input and "
                        "step outputs and create default assertions.",
                    ),
                ),
                "icon": "bi-cpu",
                "url": reverse_with_org(
                    "validations:fmu_validator_create",
                    request=self.request,
                ),
            },
            {
                "value": "shacl",
                "name": str(_("SHACL Validator")),
                "subtitle": str(_("RDF graph rules")),
                "description": str(
                    _(
                        "Bundle SHACL shapes and ontologies (ASHRAE 223P, "
                        "Brick, custom) once. Reuse across many workflows.",
                    ),
                ),
                "icon": "bi-diagram-3",
                "url": reverse_with_org(
                    "validations:shacl_library_validator_create",
                    request=self.request,
                ),
            },
        ]


class ValidatorDetailView(ValidatorLibraryMixin, DetailView):
    template_name = "validations/library/validator_detail.html"
    context_object_name = "validator"
    pk_url_kwarg = "pk"

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
        self.add_validator_version_context(context, validator)
        can_edit = (
            self.can_manage_validators()
            and not validator.is_system
            and context["is_latest_validator_version"]
        )
        context.update(
            {
                "active_tab": "description",
                "can_manage_validators": self.can_manage_validators(),
                "can_edit_validator": can_edit,
                "return_tab": self._resolve_return_tab(validator),
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
            _("View \u201c%(name)s\u201d") % {"name": label}
            if validator.is_system
            else _("Edit \u201c%(name)s\u201d") % {"name": label}
        )
        breadcrumbs.append(
            {
                "name": crumb_label,
                "url": "",
            },
        )
        return breadcrumbs


class ValidatorVersionsListView(ValidatorLibraryMixin, TemplateView):
    """Hidden manual list of historical versions for one validator family."""

    template_name = "validations/library/validator_versions.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.require_library_access():
            return redirect(
                reverse_with_org(
                    "workflows:workflow_list",
                    request=request,
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        slug = self.kwargs["slug"]
        versions = list(
            self.get_validator_queryset().filter(slug=slug).order_by("-version", "-pk"),
        )
        if not versions:
            raise Http404("No Validator matches the given query.")
        latest = versions[0]
        context.update(
            {
                "validator_family_slug": slug,
                "validator_family_name": latest.name or latest.slug,
                "versions": versions,
                "latest_validator": latest,
                "return_tab": "system" if latest.is_system else "custom",
            },
        )
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        slug = self.kwargs["slug"]
        breadcrumbs.append({"name": slug, "url": ""})
        breadcrumbs.append({"name": _("Versions"), "url": ""})
        return breadcrumbs


class ValidatorStepIOTabView(ValidatorLibraryMixin, DetailView):
    """Step inputs and outputs tab on the validator detail page."""

    model = Validator
    context_object_name = "validator"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    template_name = "validations/library/validator_detail.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.require_library_access():
            return redirect(
                reverse_with_org("workflows:workflow_list", request=request),
            )
        self.object = self.get_object()
        if self.object.is_system and not self.object.is_published:
            messages.warning(request, _("This validator is not yet available."))
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
        self.add_validator_version_context(context, validator)
        can_edit = (
            self.can_manage_validators()
            and not validator.is_system
            and context["is_latest_validator_version"]
        )

        all_io_definitions = validator.step_io_definitions.all().order_by(
            "direction",
            "order",
            "contract_key",
        )
        inputs = [
            definition
            for definition in all_io_definitions
            if definition.direction == StepIODirection.INPUT
        ]
        outputs = [
            definition
            for definition in all_io_definitions
            if definition.direction == StepIODirection.OUTPUT
        ]

        io_definition_create_form = StepIODefinitionForm(
            initial={"direction": StepIODirection.INPUT},
            validator=validator,
        )
        if not validator.has_processor:
            io_definition_create_form.fields["direction"].widget = forms.HiddenInput()

        show_output_tab = bool(validator.has_processor)
        requested_io_tab = (self.request.GET.get("step_io_tab") or "inputs").lower()
        allowed_io_tabs = {"inputs"}
        if show_output_tab:
            allowed_io_tabs.add("outputs")
        active_step_io_tab = (
            requested_io_tab if requested_io_tab in allowed_io_tabs else "inputs"
        )

        context.update(
            {
                "active_tab": "step_io",
                "can_manage_validators": self.can_manage_validators(),
                "can_edit_validator": can_edit,
                "return_tab": self._resolve_return_tab(validator),
                "inputs": inputs,
                "outputs": outputs,
                "catalog_tab_prefix": "validator-detail",
                "show_output_tab": show_output_tab,
                "active_step_io_tab": active_step_io_tab,
                "io_definition_create_form": io_definition_create_form,
                "io_definition_edit_forms": {
                    definition.pk: {
                        "form": StepIODefinitionForm(
                            instance=definition,
                            validator=validator,
                        ),
                        "title": _(
                            "Edit Step Input"
                            if definition.direction == StepIODirection.INPUT
                            else "Edit Step Output"
                        ),
                    }
                    for definition in all_io_definitions
                },
                "probe_result": (
                    getattr(validator.fmu_model, "probe_result", None)
                    if getattr(validator, "fmu_model", None)
                    else None
                ),
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
        breadcrumbs.append({"name": label, "url": ""})
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
        self.add_validator_version_context(context, validator)
        default_ruleset = validator.default_ruleset
        assertions = (
            default_ruleset.assertions.all()
            .select_related("target_io_definition")
            .order_by("order", "pk")
            if default_ruleset
            else RulesetAssertion.objects.none()
        )
        context.update(
            {
                "validator_default_assertions": assertions,
                "can_view_validator_detail": True,
            },
        )
        return context


class ValidatorAssertionsTabView(ValidatorLibraryMixin, DetailView):
    """
    Default Assertions tab on the validator detail page.

    Full-page tab view showing assertions with inline CRUD controls.
    """

    model = Validator
    context_object_name = "validator"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    template_name = "validations/library/validator_detail.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.require_library_access():
            return redirect(
                reverse_with_org("workflows:workflow_list", request=request),
            )
        self.object = self.get_object()
        if self.object.is_system and not self.object.is_published:
            messages.warning(request, _("This validator is not yet available."))
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
        self.add_validator_version_context(context, validator)
        default_ruleset = validator.default_ruleset
        default_assertions = (
            default_ruleset.assertions.all()
            .select_related("target_io_definition")
            .order_by("order", "pk")
            if default_ruleset
            else RulesetAssertion.objects.none()
        )
        io_definition_choices = [
            (io_definition.id, io_definition.contract_key)
            for io_definition in validator.step_io_definitions.order_by("contract_key")
        ]
        can_edit = (
            self.can_manage_validators()
            and not validator.is_system
            and context["is_latest_validator_version"]
        )

        context.update(
            {
                "active_tab": "assertions",
                "can_manage_validators": self.can_manage_validators(),
                "can_edit_validator": can_edit,
                "validator_default_assertions": default_assertions,
                "return_tab": self._resolve_return_tab(validator),
            },
        )

        if can_edit:
            context["default_assertion_create_form"] = ValidatorRuleForm(
                io_definition_choices=io_definition_choices,
            )
            context["default_assertion_edit_forms"] = {
                rule.id: ValidatorRuleForm(
                    initial={
                        "name": rule.name,
                        "description": rule.description,
                        "rule_type": rule.rule_type,
                        "cel_expression": rule.expression,
                        "order": rule.order,
                        "io_definitions": [
                            link.catalog_entry_id for link in rule.rule_entries.all()
                        ],
                    },
                    io_definition_choices=io_definition_choices,
                )
                for rule in default_assertions
            }
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
        validator = self.object
        label = validator.name or validator.slug
        breadcrumbs.append(
            {
                "name": label,
                "url": self.build_validator_detail_urls(validator)["description"],
            },
        )
        breadcrumbs.append({"name": _("Default Assertions"), "url": ""})
        return breadcrumbs


class ValidatorStepIOListView(ValidatorLibraryMixin, DetailView):
    """Full-page list of a validator's step I/O definitions."""

    template_name = "validations/library/validator_step_io_list.html"
    context_object_name = "validator"

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
        self.add_validator_version_context(context, validator)
        # Get all step I/O definitions ordered by direction then contract key.
        io_definitions = list(
            validator.step_io_definitions.all().order_by("direction", "contract_key"),
        )
        context.update(
            {
                "io_definitions": io_definitions,
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
                "url": self.build_validator_detail_urls(validator)["description"],
            },
        )
        breadcrumbs.append(
            {
                "name": _("Inputs & Outputs"),
                "url": "",
            },
        )
        return breadcrumbs


class ValidatorResourceFilesTabView(ValidatorLibraryMixin, DetailView):
    """
    Resource Files tab on the validator detail page.

    Visible to all users with VALIDATOR_VIEW (including Authors, read-only).
    CUD buttons are only rendered when can_manage_resource_files is True.
    """

    model = Validator
    context_object_name = "validator"
    slug_field = "slug"
    slug_url_kwarg = "slug"
    template_name = "validations/library/validator_detail.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.require_library_access():
            return redirect(
                reverse_with_org("workflows:workflow_list", request=request),
            )
        self.object = self.get_object()
        if self.object.is_system and not self.object.is_published:
            messages.warning(request, _("This validator is not yet available."))
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
        self.add_validator_version_context(context, validator)
        org = self.get_active_org()

        # Resource files visible to this org (org-specific + system-wide)
        resource_files = (
            ValidatorResourceFile.objects.filter(
                validator=validator,
            )
            .filter(
                models.Q(org=org) | models.Q(org__isnull=True),
            )
            .select_related("org", "uploaded_by")
            .order_by("-is_default", "name")
        )

        can_manage = (
            self.can_manage_resource_files() and context["is_latest_validator_version"]
        )
        resource_file_form = (
            ValidatorResourceFileForm(validator=validator) if can_manage else None
        )

        # Preload edit forms for each editable resource file
        resource_file_edit_forms = {}
        if can_manage:
            for rf in resource_files:
                if rf.org_id is not None:  # system-wide files not editable via UI
                    resource_file_edit_forms[rf.id] = ValidatorResourceFileForm(
                        instance=rf,
                        validator=validator,
                        is_edit=True,
                    )

        context.update(
            {
                "active_tab": "resource_files",
                "can_manage_validators": self.can_manage_validators(),
                "can_edit_validator": (
                    self.can_manage_validators()
                    and not validator.is_system
                    and context["is_latest_validator_version"]
                ),
                "can_manage_resource_files": can_manage,
                "resource_files": resource_files,
                "resource_file_form": resource_file_form,
                "resource_file_edit_forms": resource_file_edit_forms,
                "return_tab": self._resolve_return_tab(validator),
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
        validator = self.object
        label = validator.name or validator.slug
        breadcrumbs.append(
            {
                "name": label,
                "url": self.build_validator_detail_urls(validator)["description"],
            },
        )
        breadcrumbs.append({"name": _("Resource Files"), "url": ""})
        return breadcrumbs


class StepIODefinitionDetailView(LoginRequiredMixin, View):
    """Return modal content for a step I/O definition detail view."""

    def get(self, request, entry_pk):
        io_definition = get_object_or_404(StepIODefinition, pk=entry_pk)
        return render(
            request,
            "validations/library/partials/step_io_detail_modal_content.html",
            {
                "io_definition": io_definition,
            },
        )
