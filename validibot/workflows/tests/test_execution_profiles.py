"""Tests for workflow-author validator execution profiles.

The profile is deliberately one small semantic step setting rather than a
provider-specific infrastructure field. These tests protect the authoring
surface, canonical config storage, and the ability to switch a step back to the
stable fast-response default without leaving stale routing intent behind.
"""

import pytest

from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorExecutionProfile
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.forms import BasicStepConfigForm
from validibot.workflows.forms import EnergyPlusStepConfigForm
from validibot.workflows.forms import FMUValidatorStepConfigForm
from validibot.workflows.forms import SchematronStepConfigForm
from validibot.workflows.forms import ShaclStepConfigForm
from validibot.workflows.services.contract_snapshot import (
    compute_workflow_definition_hash,
)
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.views_helpers import save_workflow_step


def test_only_container_step_forms_expose_the_execution_profile():
    """Authors should see the choice only where separate compute is possible."""
    assert "execution_profile" in EnergyPlusStepConfigForm().fields
    assert "execution_profile" in FMUValidatorStepConfigForm().fields
    assert "execution_profile" in ShaclStepConfigForm().fields
    assert "execution_profile" in SchematronStepConfigForm().fields
    assert "execution_profile" not in BasicStepConfigForm().fields


@pytest.mark.django_db
def test_long_running_profile_is_saved_in_the_semantic_step_contract():
    """A large-work choice must survive versioning, export, and later launch."""
    workflow = WorkflowFactory()
    validator = ValidatorFactory(
        validation_type=ValidationType.FMU,
        is_system=True,
        supports_assertions=False,
    )
    form = FMUValidatorStepConfigForm(
        data={
            "name": "Large FMU simulation",
            "execution_profile": ValidatorExecutionProfile.LONG_RUNNING,
        },
        workflow=workflow,
        org=workflow.org,
        validator=validator,
    )
    assert form.is_valid(), form.errors

    step = save_workflow_step(workflow, validator, form)

    assert step.config == {
        "execution_profile": ValidatorExecutionProfile.LONG_RUNNING,
    }
    assert step.typed_config.execution_profile == (
        ValidatorExecutionProfile.LONG_RUNNING
    )


@pytest.mark.django_db
def test_switching_back_to_fast_response_removes_the_nondefault_override():
    """The default profile should stay canonical instead of accumulating noise."""
    workflow = WorkflowFactory()
    validator = ValidatorFactory(
        validation_type=ValidationType.FMU,
        is_system=True,
        supports_assertions=False,
    )
    create_form = FMUValidatorStepConfigForm(
        data={
            "name": "FMU simulation",
            "execution_profile": ValidatorExecutionProfile.LONG_RUNNING,
        },
        workflow=workflow,
        org=workflow.org,
        validator=validator,
    )
    assert create_form.is_valid(), create_form.errors
    step = save_workflow_step(workflow, validator, create_form)

    update_form = FMUValidatorStepConfigForm(
        data={
            "name": step.name,
            "execution_profile": ValidatorExecutionProfile.FAST_RESPONSE,
        },
        step=step,
        workflow=workflow,
        org=workflow.org,
        validator=validator,
    )
    assert update_form.is_valid(), update_form.errors

    updated = save_workflow_step(
        workflow,
        validator,
        update_form,
        step=step,
    )

    assert updated.config == {}
    assert updated.typed_config.execution_profile == (
        ValidatorExecutionProfile.FAST_RESPONSE
    )


@pytest.mark.django_db
def test_execution_profile_changes_the_versioned_workflow_definition_hash():
    """Evidence must distinguish fast and long routes authored for one step."""
    workflow = WorkflowFactory()
    validator = ValidatorFactory(
        validation_type=ValidationType.FMU,
        is_system=True,
    )
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        config={},
    )
    fast_hash = compute_workflow_definition_hash(workflow)

    step.config = {
        "execution_profile": ValidatorExecutionProfile.LONG_RUNNING,
    }
    step.save(update_fields=["config", "modified"])
    long_hash = compute_workflow_definition_hash(workflow)

    assert long_hash != fast_hash
