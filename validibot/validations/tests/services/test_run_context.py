"""Tests for canonical run-context construction and CEL exposure.

Cross-step execution must read ``ValidationStepRun.input_values`` and
``output_values`` directly. These tests guard the boundary that keeps workflow
execution independent from presentation summaries and ensures only completed,
earlier steps enter the ``steps.*`` namespace.
"""

from __future__ import annotations

import pytest

from validibot.actions.protocols import RunContext
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationType
from validibot.validations.services.run_context import RunContextBuilder
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.basic import BasicValidator
from validibot.workflows.tests.factories import WorkflowStepFactory

OUTPUT_TEMPERATURE = 18.5
EMISSIVITY = 0.85
PANEL_AREA = 50.0


@pytest.mark.django_db
def test_builder_reads_completed_upstream_step_values(django_assert_num_queries):
    """Earlier completed values must load through one bounded ORM query."""
    run = ValidationRunFactory()
    first_step = WorkflowStepFactory(
        workflow=run.workflow,
        order=10,
        name="Preflight",
    )
    failed_step = WorkflowStepFactory(
        workflow=run.workflow,
        order=11,
        name="Advisory check",
    )
    skipped_step = WorkflowStepFactory(
        workflow=run.workflow,
        order=12,
        name="Optional notification",
    )
    current_step = WorkflowStepFactory(
        workflow=run.workflow,
        order=20,
        name="Simulation",
    )
    ValidationStepRunFactory(
        validation_run=run,
        workflow_step=first_step,
        step_order=10,
        status=StepStatus.PASSED,
        input_values={"floor_area": 125.0},
        output_values={"warning_count": 2},
    )
    ValidationStepRunFactory(
        validation_run=run,
        workflow_step=failed_step,
        step_order=11,
        status=StepStatus.FAILED,
        output_values={"advisory_failure": True},
    )
    ValidationStepRunFactory(
        validation_run=run,
        workflow_step=skipped_step,
        step_order=12,
        status=StepStatus.SKIPPED,
    )

    with django_assert_num_queries(1):
        context = RunContextBuilder(run, current_step).build_upstream_steps()

    assert context == {
        first_step.step_key: {
            "input": {"floor_area": 125.0},
            "output": {"warning_count": 2},
        },
        failed_step.step_key: {
            "input": {},
            "output": {"advisory_failure": True},
        },
        skipped_step.step_key: {"input": {}, "output": {}},
    }


@pytest.mark.django_db
def test_builder_excludes_unfinished_and_non_upstream_steps():
    """Pending, running, current, and later rows must not leak into context."""
    run = ValidationRunFactory()
    pending_step = WorkflowStepFactory(
        workflow=run.workflow,
        order=10,
        name="Pending",
    )
    running_step = WorkflowStepFactory(
        workflow=run.workflow,
        order=15,
        name="Running",
    )
    current_step = WorkflowStepFactory(
        workflow=run.workflow,
        order=20,
        name="Current",
    )
    later_step = WorkflowStepFactory(
        workflow=run.workflow,
        order=30,
        name="Later",
    )
    for step, status in (
        (pending_step, StepStatus.PENDING),
        (running_step, StepStatus.RUNNING),
        (current_step, StepStatus.PASSED),
        (later_step, StepStatus.PASSED),
    ):
        ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
            status=status,
            output_values={"must_not_appear": True},
        )

    context = RunContextBuilder(run, current_step).build_upstream_steps()

    assert context == {}


@pytest.mark.django_db
def test_build_cel_context_exposes_canonical_upstream_outputs():
    """CEL must expose builder-provided values at the documented step path."""
    validator = ValidatorFactory(validation_type=ValidationType.BASIC)
    engine = BasicValidator()
    engine.run_context = RunContext(
        upstream_steps={
            "preflight": {
                "input": {},
                "output": {"output_temp": OUTPUT_TEMPERATURE},
            },
        },
    )

    context = engine._build_cel_context({"input": 1}, validator)

    assert context["payload"] == {"input": 1}
    assert context["steps"]["preflight"]["output"]["output_temp"] == OUTPUT_TEMPERATURE


@pytest.mark.django_db
def test_build_cel_context_exposes_workflow_signals():
    """Workflow signal values must remain available under both CEL aliases."""
    validator = ValidatorFactory(validation_type=ValidationType.BASIC)
    engine = BasicValidator()
    engine.run_context = RunContext(
        workflow_signals={"emissivity": EMISSIVITY, "panel_area": PANEL_AREA},
    )

    context = engine._build_cel_context({"input": 1}, validator)

    assert context["s"]["emissivity"] == EMISSIVITY
    assert context["s"]["panel_area"] == PANEL_AREA
    assert context["signal"] is context["s"]


def test_run_context_namespaces_default_to_empty():
    """Absent optional namespaces must be safe empty mappings, never ``None``."""
    context = RunContext()

    assert context.upstream_steps == {}
    assert context.workflow_signals == {}
    assert context.workflow_constants == {}
