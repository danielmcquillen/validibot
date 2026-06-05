import json
import logging
import uuid
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from validibot.core.mixins import BreadcrumbMixin
from validibot.core.utils import reverse_with_org
from validibot.projects.models import Project
from validibot.users.models import User
from validibot.users.permissions import PermissionCode
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.credential_utils import get_signed_credential_display_context
from validibot.validations.models import Ruleset
from validibot.validations.models import ValidationRun
from validibot.validations.services.report_layout import resolve_report_layout
from validibot.workflows.constants import WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY
from validibot.workflows.forms import WorkflowForm
from validibot.workflows.forms import WorkflowLaunchForm
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.services.version_context import build_workflow_breadcrumb_item
from validibot.workflows.services.version_context import build_workflow_version_context
from validibot.workflows.views_helpers import ensure_advanced_ruleset

logger = logging.getLogger(__name__)


def _is_parser_managed(sig) -> bool:
    """Return True when a StepIODefinition is parser-managed.

    Parser-managed rows have their value populated by the validator
    itself (via ``extract_input_signals()`` or another internal source)
    rather than by an author-configured ``StepInputBinding``. Such rows
    are unreachable from BASIC assertions because BASIC walks a dotted
    payload path; they're only valid CEL targets via ``i.<contract_key>``.

    The flag is consistent with ``views_helpers.build_step_signals_view``
    which uses the same heuristic to decide whether the UI should
    surface binding / "needs path" affordances.
    """
    return getattr(sig, "source_kind", "") == "internal" or not getattr(
        sig, "is_path_editable", True
    )


class WorkflowAccessMixin(LoginRequiredMixin, BreadcrumbMixin):
    """
    Reusable helpers for workflow UI views.

    For listing workflows, filters to the user's current org. For looking up
    specific workflows by ID (e.g., for launch), includes workflows the user
    has guest access to via WorkflowAccessGrant or public workflows.
    """

    include_tombstoned_workflows = False

    def get_workflow_queryset(self):
        """
        Get workflows for listing (scoped to current org).

        This method is used for workflow list views where we want to show
        only workflows in the user's current organization context.
        """
        user = self.request.user
        queryset = (
            Workflow.objects.for_user(user)
            .select_related("org", "user", "project")
            .prefetch_related("validation_runs")
            .order_by("name", "-version")
        )
        if not self.include_tombstoned_workflows:
            queryset = queryset.filter(is_tombstoned=False)
        current_org = None
        if hasattr(user, "get_current_org"):
            current_org = user.get_current_org()
        if current_org:
            return queryset.filter(org=current_org)
        return queryset.none()

    def get_workflow_queryset_for_access(self):
        """
        Get workflows for single-object lookup (includes guest/public access).

        This method is used when looking up a specific workflow by ID where
        we want to allow access if the user has permission via:
        1. Org membership with appropriate role
        2. Active WorkflowAccessGrant (guest access)
        3. Public workflow (is_public=True)

        Unlike get_workflow_queryset(), this does NOT filter by current_org
        so guests and public workflow users can access workflows.
        """
        user = self.request.user
        # Start with workflows accessible via membership or guest grants
        queryset = (
            Workflow.objects.for_user(user)
            .select_related("org", "user", "project")
            .prefetch_related("validation_runs")
        )
        if not self.include_tombstoned_workflows:
            queryset = queryset.filter(is_tombstoned=False)
        # Also include public workflows (any authenticated user can access)
        # Use union to combine distinct querysets properly
        from django.db.models import Q

        public_qs = (
            Workflow.objects.filter(
                Q(is_public=True)
                & Q(is_active=True)
                & Q(is_archived=False)
                & Q(is_tombstoned=False)
            )
            .select_related("org", "user", "project")
            .distinct()
        )
        return queryset | public_qs

    def get_queryset(self):
        return self.get_workflow_queryset()

    def user_can_manage_workflow(self, *, user: User | None = None) -> bool:
        user = user or self.request.user
        if not getattr(user, "is_authenticated", False):
            return False
        membership = user.membership_for_current_org()
        if membership is None or not membership.is_active:
            return False
        return user.has_perm(PermissionCode.WORKFLOW_EDIT.value, membership.org)

    def user_can_view_workflow(self, *, user: User | None = None) -> bool:
        user = user or self.request.user
        if not getattr(user, "is_authenticated", False):
            return False
        membership = user.membership_for_current_org()
        if membership is None or not membership.is_active:
            return False
        return user.has_perm(PermissionCode.WORKFLOW_VIEW.value, membership.org)

    def user_can_create_workflow(self, *, user: User | None = None) -> bool:
        user = user or self.request.user
        if not getattr(user, "is_authenticated", False):
            return False
        membership = user.membership_for_current_org()
        if membership is None or not membership.is_active:
            return False
        return user.has_perm(PermissionCode.WORKFLOW_EDIT.value, membership.org)

    def _can_manage_workflow_actions(
        self,
        workflow: Workflow,
        user: User,
        membership,
    ) -> bool:
        """
        Archive/delete permissions: Owners/Admins can manage any workflow;
        Authors can manage only workflows they created.
        """
        if not membership or not getattr(membership, "is_active", False):
            return False
        if user.has_perm(PermissionCode.ADMIN_MANAGE_ORG.value, workflow):
            return True
        if not user.has_perm(PermissionCode.WORKFLOW_EDIT.value, workflow):
            return False
        return workflow.user_id == getattr(user, "id", None)

    def workflow_breadcrumb_item(
        self,
        workflow: Workflow,
        *,
        url: str = "",
    ) -> dict[str, Any]:
        """Return the standard workflow-name breadcrumb with version context."""

        return build_workflow_breadcrumb_item(workflow=workflow, url=url)

    def user_can_manage_sharing(
        self,
        workflow: Workflow | None = None,
        *,
        user: User | None = None,
    ) -> bool:
        """
        Sharing permissions: Admins can share any workflow in the org;
        Authors can only share workflows they created.
        """
        user = user or self.request.user
        if workflow is None:
            workflow = self.get_workflow() if hasattr(self, "get_workflow") else None
        if workflow is None:
            return False
        if not getattr(user, "is_authenticated", False):
            return False
        membership = user.membership_for_current_org()
        if membership is None or not membership.is_active:
            return False
        # Admins can share any workflow
        if user.has_perm(PermissionCode.ADMIN_MANAGE_ORG.value, workflow):
            return True
        # Authors can share their own workflows if they have edit permission
        if not user.has_perm(PermissionCode.WORKFLOW_EDIT.value, workflow):
            return False
        return workflow.user_id == getattr(user, "id", None)


class WorkflowObjectMixin(WorkflowAccessMixin):
    """Resolve a workflow object and attach version-family context.

    Workflow pages generally operate on one concrete workflow version. The
    mixin keeps that exact-row lookup in one place and also gives templates the
    version-family context they need to show the detail-page version history
    without each view repeating the same query.
    """

    workflow_url_kwarg = "pk"

    def get_workflow(self) -> Workflow:
        """
        Get a single workflow by ID, checking all access paths.

        Uses get_workflow_queryset_for_access() to include workflows the user
        can access via membership, guest grants, or public access - not just
        workflows in their current org.
        """
        if not hasattr(self, "_workflow"):
            queryset = (
                self.get_workflow_queryset_for_access()
                .select_related("org", "user", "project")
                .prefetch_related("steps")
            )
            workflow_id = self.kwargs.get(self.workflow_url_kwarg)
            self._workflow = get_object_or_404(queryset, pk=workflow_id)
        return self._workflow

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = context.get("workflow") or self.get_workflow()
        context["workflow"] = workflow
        context.update(
            build_workflow_version_context(
                request=self.request,
                workflow=workflow,
            ),
        )
        return context


class WorkflowStepAssertionsMixin(WorkflowObjectMixin):
    """Shared helpers for assertion management views."""

    def dispatch(self, request, *args, **kwargs):
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        if not self._supports_assertions():
            messages.error(
                request,
                _("Assertions are only available for advanced validators."),
            )
            return HttpResponseRedirect(
                reverse_with_org(
                    "workflows:workflow_detail",
                    request=request,
                    kwargs={"pk": self.get_workflow().pk},
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def _supports_assertions(self) -> bool:
        validator = getattr(self.step, "validator", None)
        if not validator:
            return False
        return validator.supports_assertions

    def get_ruleset(self) -> Ruleset:
        validator = self.step.validator
        ruleset = getattr(self.step, "ruleset", None)
        if ruleset is None and validator is not None:
            ruleset = ensure_advanced_ruleset(
                self.get_workflow(),
                self.step,
                validator,
            )
        return ruleset

    def get_catalog_choices(self, stage: str | None = None):
        """Build the assertion-target autocomplete list, optionally filtered by stage.

        Per ADR-2026-05-22, the autocomplete is stage-aware:

        - ``stage="input"`` — input-stage assertion: shows ``p.*``,
          ``s.*``, ``i.*``, and upstream steps via ``steps.<key>.*``,
          but NOT this step's ``o.*`` (the validator hasn't run yet).
        - ``stage="output"`` (or ``None`` for backward compatibility) —
          output-stage assertion: shows everything including this step's
          ``o.*`` and ``i.*``.

        Cache key includes stage so input- and output-stage callers
        don't collide.
        """
        cache_key = f"_catalog_choice_cache_{stage or 'all'}"
        if hasattr(self, cache_key):
            return getattr(self, cache_key)
        from validibot.validations.constants import SignalDirection
        from validibot.validations.models import StepIODefinition
        from validibot.workflows.models import WorkflowSignalMapping

        validator = self.step.validator
        choices: list[tuple[str, str]] = []
        signal_defs: list = []
        include_outputs = stage != "input"  # input-stage excludes this step's o.*
        include_inputs = True  # i.* available at both stages

        # ── This validator's catalog-declared step inputs and outputs ──
        # Step inputs (direction=INPUT) populate i.* via the validator's
        # extract_input_signals() parser hook or via resolved bindings.
        # Step outputs (direction=OUTPUT) populate o.* after the
        # validator's process runs.
        #
        # Parser-managed inputs (source_kind=internal or
        # is_path_editable=False) get a "(CEL only)" suffix in their
        # autocomplete label — BASIC evaluators can't reach them, so
        # surfacing that here heads off the UX trap where an author
        # picks ``i.zone_count`` for a BASIC assertion and only
        # discovers it's unreachable at form submission time
        # (rejected by RulesetAssertionForm.clean). See the May 2026
        # review's P2 finding.
        if validator:
            signal_defs = list(
                validator.signal_definitions.order_by("order", "contract_key")
            )
            for sig in signal_defs:
                if sig.direction == SignalDirection.OUTPUT and include_outputs:
                    choices.append(
                        (
                            f"o.{sig.contract_key}",
                            f"{sig.label or sig.contract_key} · {_('Step output')}",
                        ),
                    )
                elif sig.direction == SignalDirection.INPUT and include_inputs:
                    base_label = f"{sig.label or sig.contract_key} · {_('Step input')}"
                    if _is_parser_managed(sig):
                        base_label = f"{base_label} ({_('CEL only')})"
                    choices.append((f"i.{sig.contract_key}", base_label))

        # ── Step-owned catalog entries (e.g. FMU probe results, template scans) ──
        step_sigs = list(self.step.signal_definitions.order_by("order", "contract_key"))
        seen = {(sig.contract_key, sig.direction) for sig in signal_defs}
        for sig in step_sigs:
            if (sig.contract_key, sig.direction) in seen:
                continue
            seen.add((sig.contract_key, sig.direction))
            display_name = sig.label or sig.native_name or sig.contract_key
            if sig.direction == SignalDirection.OUTPUT and include_outputs:
                choices.append(
                    (f"o.{sig.contract_key}", f"{display_name} · {_('Step output')}"),
                )
            elif sig.direction == SignalDirection.INPUT and include_inputs:
                base_label = f"{display_name} · {_('Step input')}"
                if _is_parser_managed(sig):
                    base_label = f"{base_label} ({_('CEL only')})"
                choices.append((f"i.{sig.contract_key}", base_label))
            signal_defs.append(sig)

        # ── Workflow-level signal mappings appear as s.<name> ──
        # Available at both stages.
        workflow = self.step.workflow
        seen_signal_names: set[str] = set()
        for mapping in (
            WorkflowSignalMapping.objects.filter(workflow=workflow)
            .order_by("position")
            .values("name")
        ):
            name = mapping["name"]
            seen_signal_names.add(name)
            choices.append((f"s.{name}", f"{name} · {_('Signal')}"))

        # ── Promoted step inputs/outputs from upstream steps ──
        # Per ADR-2026-05-22b's temporal rule, promoted values from
        # upstream steps (any direction) live in s.* and are visible to
        # this step at both input and output stages.
        #
        # Two sources, matching the runtime injection in
        # _inject_promotions:
        #
        # 1. In-row promotions on step-owned StepIODefinitions.
        # 2. WorkflowStepIOPromotion overlays on validator-owned
        #    StepIODefinitions (May 2026 P1 fix).
        from validibot.validations.models import WorkflowStepIOPromotion

        promoted_step_owned = (
            StepIODefinition.objects.filter(
                workflow_step__workflow=workflow,
                workflow_step__order__lt=self.step.order,
            )
            .exclude(promoted_signal_name="")
            .values_list("promoted_signal_name", "direction")
        )
        promoted_overlay = WorkflowStepIOPromotion.objects.filter(
            workflow_step__workflow=workflow,
            workflow_step__order__lt=self.step.order,
        ).values_list(
            "promoted_signal_name",
            "signal_definition__direction",
        )

        for signal_name, direction in list(promoted_step_owned) + list(
            promoted_overlay,
        ):
            if signal_name not in seen_signal_names:
                seen_signal_names.add(signal_name)
                source_label = (
                    _("Promoted step input")
                    if direction == SignalDirection.INPUT
                    else _("Promoted step output")
                )
                choices.append(
                    (f"s.{signal_name}", f"{signal_name} · {source_label}"),
                )

        # ── Submission envelope appears as submission.* (ADR-2026-06-03b) ──
        # Available at BOTH stages and for every validator regardless of file
        # format — the envelope (submitter metadata + server facts) lives
        # beside the file. We enumerate the FIXED fields; the free-form
        # metadata bag has arbitrary keys that can't be listed, so we offer a
        # single hint entry the author completes with their own key.
        submission_label = _("Submission")
        choices.extend(
            [
                (
                    "submission.metadata.",
                    f"{_('Submission metadata — add your key')} · {submission_label}",
                ),
                ("submission.name", f"{_('Submission name')} · {submission_label}"),
                (
                    "submission.short_description",
                    f"{_('Submission description')} · {submission_label}",
                ),
                (
                    "submission.original_filename",
                    f"{_('Original filename')} · {submission_label}",
                ),
                ("submission.file_type", f"{_('File type')} · {submission_label}"),
                ("submission.size", f"{_('File size (bytes)')} · {submission_label}"),
                ("submission.uploaded_at", f"{_('Uploaded at')} · {submission_label}"),
            ],
        )

        self._catalog_entries_cache = signal_defs
        setattr(self, cache_key, choices)
        self._workflow_signal_names_cache = seen_signal_names
        return choices

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()
        context.update(
            {
                "workflow": workflow,
                "step": self.step,
                "validator": self.step.validator,
                "ruleset": self.get_ruleset(),
                "assertions": self.get_ruleset()
                .assertions.all()
                .order_by("order", "pk"),
                "can_manage_assertions": self.user_can_manage_workflow(),
            },
        )
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        workflow = self.get_workflow()
        breadcrumbs.append(
            self.workflow_breadcrumb_item(
                workflow,
                url=reverse_with_org(
                    "workflows:workflow_detail",
                    request=self.request,
                    kwargs={"pk": workflow.pk},
                ),
            ),
        )
        breadcrumbs.append({"name": _("Assertions"), "url": ""})
        return breadcrumbs


class WorkflowLaunchContextMixin(WorkflowObjectMixin):
    """
    This mixin provides helper methods to build context for launching workflows
    via the UI. It also provides methods to get recent runs and load a specific run
    for display.

    Args:
        WorkflowObjectMixin (_type_): _description_

    Returns:
        _type_: _description_
    """

    launch_panel_template_name = "workflows/launch/partials/launch_panel.html"

    run_detail_template_name = "workflows/launch/workflow_run_detail.html"
    run_detail_panel_template_name = "workflows/launch/partials/run_status_card.html"
    status_area_template_name = "workflows/launch/partials/run_status_card.html"

    polling_statuses = {
        ValidationRunStatus.PENDING,
        ValidationRunStatus.RUNNING,
    }

    def get_poll_interval_seconds(self) -> int:
        return int(getattr(settings, "WORKFLOW_RUN_POLL_INTERVAL_SECONDS", 3))

    def _collect_run_display_data(
        self,
        run: ValidationRun | None,
    ) -> tuple[list[Any], list[Any], bool]:
        if not run:
            return [], [], False
        step_runs = list(
            run.step_runs.select_related(
                "workflow_step",
                "workflow_step__validator",
            )
            .prefetch_related("findings", "findings__ruleset_assertion")
            .order_by("step_order"),
        )
        run_in_progress = run.status in self.polling_statuses
        findings: list[Any] = []
        if not run_in_progress:
            findings = list(
                run.findings.select_related(
                    "validation_step_run",
                    "validation_step_run__workflow_step",
                ).order_by("severity", "-created")[:10],
            )
        return step_runs, findings, run_in_progress

    def get_displayable_run_queryset(self, workflow: Workflow):
        """Return workflow runs the current user may inspect from launch UI."""
        user = self.request.user
        base_qs = ValidationRun.objects.filter(workflow=workflow)
        if not getattr(user, "is_authenticated", False):
            return base_qs.none()
        if user.has_perm(
            PermissionCode.VALIDATION_RESULTS_VIEW_ALL.value,
            workflow.org,
        ):
            return base_qs
        if user.has_perm(
            PermissionCode.VALIDATION_RESULTS_VIEW_OWN.value,
            workflow.org,
        ):
            return base_qs.filter(user=user)
        # Public workflow and guest-grant launchers may not be org members.
        # They can still inspect the runs they personally launched.
        return base_qs.filter(user=user)

    def user_can_cancel_run(self, run: ValidationRun) -> bool:
        """Limit cancellation to the launcher or an org administrator."""
        user = self.request.user
        if not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        if run.user_id and run.user_id == getattr(user, "id", None):
            return True
        return user.has_perm(PermissionCode.ADMIN_MANAGE_ORG.value, run.org)

    def build_status_area_context(
        self,
        *,
        workflow: Workflow,
        active_run: ValidationRun | None,
    ) -> dict[str, object]:
        step_runs, findings, run_in_progress = self._collect_run_display_data(
            active_run,
        )
        poll_interval = self.get_poll_interval_seconds()
        run_detail_url = None
        run_detail_absolute_url = None
        detail_url = None
        cancel_url = None
        if active_run:
            run_detail_url = reverse_with_org(
                "workflows:workflow_run_detail",
                request=self.request,
                kwargs={"pk": workflow.pk, "run_id": active_run.pk},
            )
            run_detail_absolute_url = self.request.build_absolute_uri(run_detail_url)
            detail_url = reverse_with_org(
                "validations:validation_detail",
                request=self.request,
                kwargs={"pk": active_run.pk},
            )
            if run_in_progress and self.user_can_cancel_run(active_run):
                cancel_url = reverse_with_org(
                    "workflows:workflow_launch_cancel",
                    request=self.request,
                    kwargs={"pk": workflow.pk, "run_id": active_run.pk},
                )
        launch_url = reverse_with_org(
            "workflows:workflow_launch",
            request=self.request,
            kwargs={"pk": workflow.pk},
        )
        previous_runs_url = reverse_with_org(
            "workflows:workflow_validation_list",
            request=self.request,
            kwargs={"pk": workflow.pk},
        )
        # Build signal/param display data for completed runs.
        from validibot.validations.services.signal_display import (
            build_display_step_outputs,
        )
        from validibot.validations.services.signal_display import (
            build_template_params_display,
        )

        step_signals: dict[int, list] = {}
        step_params: dict[int, list] = {}
        step_template_warnings: dict[int, list] = {}
        if not run_in_progress:
            for sr in step_runs:
                signals = build_display_step_outputs(sr)
                if signals:
                    step_signals[sr.pk] = signals
                params = build_template_params_display(sr)
                if params:
                    step_params[sr.pk] = params
                warnings = (sr.output or {}).get("template_warnings")
                if warnings:
                    step_template_warnings[sr.pk] = warnings

        # Flatten all signals across steps for the "Workflow Outputs" summary.
        all_signals = [
            signal for signals in step_signals.values() for signal in signals
        ]

        credential_context = {
            "issued_credential": None,
            "credential_download_url": None,
            "credential_download_name": None,
            "credential_resource_label": None,
        }
        if active_run and not run_in_progress:
            credential_context = get_signed_credential_display_context(
                request=self.request,
                run=active_run,
            )

        context = {
            "active_run": active_run,
            "step_runs": step_runs,
            "findings": findings,
            "run_in_progress": run_in_progress,
            "polling_statuses": self.polling_statuses,
            "poll_interval_seconds": poll_interval,
            "status_url": run_detail_url,
            "run_detail_refresh_url": run_detail_url,
            "run_detail_absolute_url": run_detail_absolute_url,
            "detail_url": detail_url,
            "cancel_url": cancel_url,
            "launch_url": launch_url,
            "previous_runs_url": previous_runs_url,
            "step_signals": step_signals,
            "has_signals": bool(step_signals),
            "all_signals": all_signals,
            "step_params": step_params,
            "step_template_warnings": step_template_warnings,
        }
        context.update(credential_context)
        return context

    def get_recent_runs(self, workflow: Workflow, limit: int = 5):
        return list(
            self.get_displayable_run_queryset(workflow)
            .select_related("submission", "user")
            .order_by("-created")[:limit],
        )

    def _remember_launch_input_mode(
        self,
        request,
        payload: str | None,
        mode: str | None = None,
    ) -> None:
        selected_mode = mode if mode in {"upload", "paste"} else None
        if not selected_mode:
            selected_mode = "paste" if (payload or "").strip() else "upload"
        try:
            request.session[WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY] = selected_mode
            request.session.modified = True
        except Exception:  # pragma: no cover - defensive
            logger.exception("Unable to persist workflow launch input mode preference.")

    def get_launch_form(
        self,
        *,
        workflow: Workflow,
        data=None,
        files=None,
    ) -> WorkflowLaunchForm:
        return WorkflowLaunchForm(
            data=data,
            files=files,
            workflow=workflow,
            user=self.request.user,
        )

    def load_run_for_display(
        self,
        *,
        workflow: Workflow,
        run_id,
    ) -> ValidationRun | None:
        if not run_id:
            return None
        try:
            uuid_val = (
                run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
            )
        except (TypeError, ValueError):
            return None
        return (
            self.get_displayable_run_queryset(workflow)
            .filter(pk=uuid_val)
            .select_related("submission", "user")
            .prefetch_related(
                "step_runs",
                "step_runs__workflow_step",
                "step_runs__findings",
                "findings",
                "findings__ruleset_assertion",
            )
            .first()
        )

    def build_run_detail_context(
        self,
        *,
        workflow: Workflow,
        run: ValidationRun,
    ) -> dict[str, object]:
        status_context = self.build_status_area_context(
            workflow=workflow,
            active_run=run,
        )
        submission_content = ""
        submission_content_can_be_viewed = False
        if run.submission:
            submission_content = run.submission.get_viewable_content()
            submission_content_can_be_viewed = bool(submission_content)

        context = {
            "workflow": workflow,
            "run": run,
            "active_run": run,
            "panel_mode": "status",
            "can_execute": workflow.can_execute(user=self.request.user),
            "has_steps": workflow.steps.exists(),
            "recent_runs": self.get_recent_runs(workflow),
            "is_polling": run.status in self.polling_statuses,
            "submission_content": submission_content,
            "submission_content_can_be_viewed": submission_content_can_be_viewed,
            # Stacked vs classic report layout — session-remembered, default
            # stacked. Shared with the standalone run-detail page so the launch
            # card and the detail page render the same layout.
            "report_layout": resolve_report_layout(self.request),
        }
        context.update(status_context)
        return context

    def render_run_detail_panel(
        self,
        request,
        *,
        workflow: Workflow,
        run: ValidationRun,
        status_code: int,
        toast: dict[str, str] | None = None,
    ):
        is_htmx = request.headers.get("HX-Request") == "true"
        context = self.build_run_detail_context(workflow=workflow, run=run)
        template_name = (
            self.run_detail_panel_template_name
            if is_htmx
            else self.run_detail_template_name
        )
        response = render(
            request,
            template_name,
            context=context,
            status=status_code,
        )
        if is_htmx:
            response["HX-Retarget"] = "#workflow-run-detail-panel"

        if toast:
            sanitized_toast = {
                key: str(value) if isinstance(value, Promise) else value
                for key, value in toast.items()
            }
            response["HX-Trigger"] = json.dumps({"toast": sanitized_toast})
        return response


class WorkflowFormViewMixin(WorkflowAccessMixin):
    form_class = WorkflowForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def _default_project_for_org(self) -> Project | None:
        user = getattr(self.request, "user", None)
        org = getattr(user, "get_current_org", lambda: None)() if user else None
        if not org:
            return None
        project = Project.objects.filter(org=org, is_default=True).first()
        if project:
            return project
        return Project.objects.filter(org=org).order_by("name").first()
