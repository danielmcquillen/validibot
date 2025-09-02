from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from roscoe.validations.models import ValidationRun
from roscoe.workflows.models import Workflow


@dataclass
class ExecutionPlan:
    workflow_id: int
    steps: list[int]
    context: dict[str, Any]


class ExecutionPlanBuilder:
    def build(self, workflow: Workflow, payload: dict) -> ExecutionPlan:
        steps_qs = getattr(workflow, "steps", None)
        step_ids = (
            list(
                steps_qs.filter(is_enabled=True)
                .order_by("order")
                .values_list("id", flat=True),
            )
            if steps_qs is not None
            else []
        )
        return ExecutionPlan(
            workflow_id=workflow.id,
            steps=step_ids,
            context={"payload": payload},
        )


class ValidationRunner:
    def __init__(self, plan_builder: ExecutionPlanBuilder | None = None):
        self.plan_builder = plan_builder or ExecutionPlanBuilder()

    def run(self, run: ValidationRun, payload: dict) -> dict:
        plan = self.plan_builder.build(run.workflow, payload)
        # TODO: execute real steps, collect artifacts/findings
        return {
            "summary": (
                f"Executed {len(plan.steps)} step(s) for workflow {plan.workflow_id}.",
            ),
            "steps": plan.steps,
            "context_used": plan.context,
        }
