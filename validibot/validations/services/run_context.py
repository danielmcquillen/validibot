"""Canonical runtime context construction for workflow execution.

Execution reads step values from ``ValidationStepRun`` records. Presentation
summaries are deliberately absent from this service so rebuilding or changing
a UI/API projection cannot alter the values seen by later workflow steps.
"""

from __future__ import annotations

import json
from typing import Any

from validibot.actions.protocols import RunContext
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import StepStatus
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.signal_resolution import resolve_workflow_signals
from validibot.workflows.services.constants import build_workflow_constants_context


class RunContextBuilder:
    """Build the one authoritative context passed to a workflow step.

    The builder composes workflow-scoped values and completed upstream step
    values from their canonical domain records. Keeping composition here gives
    validator and action execution the same namespace rules and query path.

    Args:
        validation_run: Run currently being executed.
        workflow_step: Workflow step about to execute.
    """

    def __init__(self, validation_run: Any, workflow_step: Any) -> None:
        self.validation_run = validation_run
        self.workflow_step = workflow_step

    def build(self) -> RunContext:
        """Return the complete context for the current workflow step."""
        return RunContext(
            validation_run=self.validation_run,
            step=self.workflow_step,
            upstream_steps=self.build_upstream_steps(),
            workflow_signals=self._resolve_workflow_signals(),
            workflow_constants=build_workflow_constants_context(
                getattr(self.workflow_step, "workflow", None),
            ),
        )

    def build_upstream_steps(self) -> dict[str, dict[str, Any]]:
        """Return canonical values from completed earlier step runs.

        Failed and skipped steps are included because advisory workflow steps
        may allow execution to continue. Pending/running steps and the current
        step are never visible. A missing or duplicate stable key is rejected
        rather than silently creating an ambiguous CEL namespace.
        """
        current_order = getattr(self.workflow_step, "order", None)
        if current_order is None:
            return {}

        step_runs = (
            ValidationStepRun.objects.filter(
                validation_run=self.validation_run,
                step_order__lt=current_order,
                status__in=(
                    StepStatus.PASSED,
                    StepStatus.FAILED,
                    StepStatus.SKIPPED,
                ),
            )
            .select_related("workflow_step")
            .order_by("step_order", "pk")
        )

        upstream_steps: dict[str, dict[str, Any]] = {}
        for step_run in step_runs:
            step_key = step_run.workflow_step.step_key
            if not step_key:
                msg = (
                    f"Workflow step {step_run.workflow_step_id} has no stable step_key."
                )
                raise ValueError(msg)
            if step_key in upstream_steps:
                msg = f"Duplicate upstream workflow step key: {step_key!r}."
                raise ValueError(msg)
            upstream_steps[step_key] = {
                "input": step_run.input_values or {},
                "output": step_run.output_values or {},
            }
        return upstream_steps

    def _resolve_workflow_signals(self) -> dict[str, Any]:
        """Resolve the workflow's ``s.*`` values against submission data."""
        workflow = getattr(self.workflow_step, "workflow", None)
        workflow_pk = getattr(workflow, "pk", None)
        if not workflow or not isinstance(workflow_pk, int):
            return {}

        from validibot.workflows.models import WorkflowSignalMapping

        if not WorkflowSignalMapping.objects.filter(workflow=workflow).exists():
            return {}

        submission = getattr(self.validation_run, "submission", None)
        if not submission:
            return {}
        raw = submission.get_content()
        if not raw:
            return {}

        file_type = getattr(submission, "file_type", SubmissionFileType.JSON)
        if file_type == SubmissionFileType.XML:
            try:
                from validibot.validations.xml_utils import xml_to_dict

                submission_data = xml_to_dict(raw)
            except Exception:
                return {}
        else:
            try:
                submission_data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}

        return resolve_workflow_signals(workflow, submission_data).signals
