"""Validator library browsing: listing, detail, signals, assertions,
and signal definition views.
"""

import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import models
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
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.forms import SignalDefinitionForm
from validibot.validations.forms import ValidatorResourceFileForm
from validibot.validations.forms import ValidatorRuleForm
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import SignalDefinition
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
            Validator.objects.filter(is_enabled=True)
            .select_related("custom_validator", "org")
            .prefetch_related(
                "signal_definitions",
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
                    is_system=True,
                    is_enabled=True,
                )
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
                "value": "fmu",
                "name": str(_("FMU Validator")),
                "subtitle": str(_("Simulation-based")),
                "description": str(
                    _(
                        "Upload an FMU to auto-discover input and "
                        "output signals and create default assertions.",
                    ),
                ),
                "icon": "bi-cpu",
                "url": reverse_with_org(
                    "validations:fmu_validator_create",
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
        context.update(
            {
                "active_tab": "description",
                "can_manage_validators": self.can_manage_validators(),
                "can_edit_validator": self.can_manage_validators()
                and not validator.is_system,
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


class ValidatorSignalsTabView(ValidatorLibraryMixin, DetailView):
    """Signals tab on the validator detail page."""

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
        can_edit = self.can_manage_validators() and not validator.is_system

        all_signals = validator.signal_definitions.all().order_by(
            "direction",
            "order",
            "contract_key",
        )
        inputs = [s for s in all_signals if s.direction == SignalDirection.INPUT]
        outputs = [s for s in all_signals if s.direction == SignalDirection.OUTPUT]

        signal_create_form = SignalDefinitionForm(
            initial={"direction": SignalDirection.INPUT},
            validator=validator,
        )
        if not validator.has_processor:
            signal_create_form.fields["direction"].widget = forms.HiddenInput()

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
                "active_tab": "signals",
                "can_manage_validators": self.can_manage_validators(),
                "can_edit_validator": can_edit,
                "return_tab": self._resolve_return_tab(validator),
                "inputs": inputs,
                "outputs": outputs,
                "catalog_tab_prefix": "validator-detail",
                "show_output_tab": show_output_tab,
                "active_signals_tab": active_signals_tab,
                "signal_create_form": signal_create_form,
                "signal_edit_forms": {
                    signal.pk: {
                        "form": SignalDefinitionForm(
                            instance=signal,
                            validator=validator,
                        ),
                        "title": _(
                            "Edit Input Signal"
                            if signal.direction == SignalDirection.INPUT
                            else "Edit Output Signal"
                        ),
                    }
                    for signal in all_signals
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
        default_ruleset = validator.default_ruleset
        assertions = (
            default_ruleset.assertions.all()
            .select_related("target_signal_definition")
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
        default_ruleset = validator.default_ruleset
        default_assertions = (
            default_ruleset.assertions.all()
            .select_related("target_signal_definition")
            .order_by("order", "pk")
            if default_ruleset
            else RulesetAssertion.objects.none()
        )
        signal_choices = [
            (sig.id, sig.contract_key)
            for sig in validator.signal_definitions.order_by("contract_key")
        ]
        can_edit = self.can_manage_validators() and not validator.is_system

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
                "url": reverse_with_org(
                    "validations:validator_detail",
                    request=self.request,
                    kwargs={"slug": validator.slug},
                ),
            },
        )
        breadcrumbs.append({"name": _("Default Assertions"), "url": ""})
        return breadcrumbs


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
        # Get all signals ordered by direction then contract_key
        signals = list(
            validator.signal_definitions.all().order_by("direction", "contract_key"),
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

        can_manage = self.can_manage_resource_files()
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
                    self.can_manage_validators() and not validator.is_system
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
                "url": reverse_with_org(
                    "validations:validator_detail",
                    request=self.request,
                    kwargs={"slug": validator.slug},
                ),
            },
        )
        breadcrumbs.append({"name": _("Resource Files"), "url": ""})
        return breadcrumbs


class CatalogEntryDetailView(LoginRequiredMixin, View):
    """Return modal content for a signal definition detail view."""

    def get(self, request, entry_pk):
        signal = get_object_or_404(SignalDefinition, pk=entry_pk)
        return render(
            request,
            "validations/library/partials/signal_detail_modal_content.html",
            {
                "signal": signal,
            },
        )
