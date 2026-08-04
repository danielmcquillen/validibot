"""Domain tests for workflow editing-policy transitions and live-use fencing.

These tests keep the trust boundary below the HTML form: policy changes and
semantic writes must serialize against run admission even when invoked from a
service, model, import path, or future API.
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.constants import WorkflowHistoryPolicy
from validibot.workflows.services.editing_policy import EditingPolicyFixedError
from validibot.workflows.services.editing_policy import WorkflowDefinitionInUseError
from validibot.workflows.services.editing_policy import change_editing_policy
from validibot.workflows.services.editing_policy import (
    guard_workflow_definition_mutation,
)
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def test_workflow_defaults_to_versioned_editing_policy():
    """Authors must explicitly opt into the weaker Mutable history promise."""

    workflow = WorkflowFactory()

    assert workflow.history_policy == WorkflowHistoryPolicy.VERSIONED


@pytest.mark.parametrize(
    ("initial", "proposed"),
    [
        (WorkflowHistoryPolicy.VERSIONED, WorkflowHistoryPolicy.MUTABLE),
        (WorkflowHistoryPolicy.MUTABLE, WorkflowHistoryPolicy.VERSIONED),
    ],
)
def test_unused_workflow_can_change_policy_in_either_direction(initial, proposed):
    """Before a historical boundary exists, the select remains a real choice."""

    workflow = WorkflowFactory(history_policy=initial, is_locked=False)

    changed = change_editing_policy(
        workflow=workflow,
        proposed_policy=proposed,
        actor=workflow.user,
    )

    assert changed.history_policy == proposed


@pytest.mark.parametrize("boundary", ["run", "lock"])
def test_ordinary_author_cannot_change_policy_after_boundary(boundary):
    """Runs and explicit locks both permanently fix this version's policy."""

    workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.VERSIONED)
    if boundary == "run":
        ValidationRunFactory(workflow=workflow)
    else:
        workflow.is_locked = True
        workflow.save(update_fields=["is_locked"])

    with pytest.raises(EditingPolicyFixedError) as exc_info:
        change_editing_policy(
            workflow=workflow,
            proposed_policy=WorkflowHistoryPolicy.MUTABLE,
            actor=workflow.user,
        )

    assert exc_info.value.code == "editing_policy_fixed"


def test_versioned_superuser_repair_can_join_the_live_definition_fence():
    """A Versioned repair override must not permit a mixed-definition run."""

    workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.VERSIONED)
    workflow.user.is_superuser = True
    workflow.user.save(update_fields=["is_superuser"])
    ValidationRunFactory(workflow=workflow)

    with (
        pytest.raises(WorkflowDefinitionInUseError),
        guard_workflow_definition_mutation(
            workflow.pk,
            include_versioned_definition_users=True,
        ),
    ):
        pass


def test_superuser_can_change_a_fixed_policy_for_audited_repair():
    """The existing break-glass policy transition remains deliberately available."""

    workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.MUTABLE)
    workflow.user.is_superuser = True
    workflow.user.save(update_fields=["is_superuser"])
    ValidationRunFactory(workflow=workflow)

    changed = change_editing_policy(
        workflow=workflow,
        proposed_policy=WorkflowHistoryPolicy.VERSIONED,
        actor=workflow.user,
    )

    assert changed.history_policy == WorkflowHistoryPolicy.VERSIONED


@pytest.mark.parametrize(
    "status",
    [
        ValidationRunStatus.PENDING,
        ValidationRunStatus.RUNNING,
        ValidationRunStatus.FAILED,
    ],
)
def test_unreleased_run_blocks_mutable_semantic_mutation_regardless_of_status(status):
    """Terminal status alone is insufficient until finalizers release definition use."""

    workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.MUTABLE)
    ValidationRunFactory(workflow=workflow, status=status)

    with (
        pytest.raises(WorkflowDefinitionInUseError) as exc_info,
        guard_workflow_definition_mutation(workflow.pk),
    ):
        pass

    assert exc_info.value.active_run_count == 1
    assert exc_info.value.code == "workflow_definition_in_use"


def test_all_runs_must_release_before_mutable_semantic_mutation():
    """One released run cannot hide another run that still reads the definition."""

    workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.MUTABLE)
    ValidationRunFactory(workflow=workflow, definition_released_at=timezone.now())
    ValidationRunFactory(workflow=workflow)

    with (
        pytest.raises(WorkflowDefinitionInUseError) as exc_info,
        guard_workflow_definition_mutation(workflow.pk),
    ):
        pass

    assert exc_info.value.active_run_count == 1


def test_released_history_allows_mutable_semantic_mutation():
    """Completed finalization re-enables the intentionally convenient edit path."""

    workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.MUTABLE)
    ValidationRunFactory(workflow=workflow, definition_released_at=timezone.now())

    with guard_workflow_definition_mutation(workflow.pk):
        workflow.allowed_file_types = ["json", "text"]
        workflow.save(update_fields=["allowed_file_types"])

    workflow.refresh_from_db()
    assert workflow.allowed_file_types == ["json", "text"]


def test_cosmetic_mutation_bypasses_live_definition_check():
    """Authors can fix presentation copy while a Mutable validation is running."""

    workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.MUTABLE)
    ValidationRunFactory(workflow=workflow)

    with guard_workflow_definition_mutation(workflow.pk, semantic_change=False):
        workflow.description = "Clearer wording"
        workflow.save(update_fields=["description"])

    workflow.refresh_from_db()
    assert workflow.description == "Clearer wording"


def test_step_model_cannot_bypass_live_definition_fence():
    """A direct ORM step write is rejected just like a settings-form change."""

    workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.MUTABLE)
    step = WorkflowStepFactory(workflow=workflow)
    ValidationRunFactory(workflow=workflow)
    step.config = {"case_sensitive": False}

    with pytest.raises(WorkflowDefinitionInUseError):
        step.save(update_fields=["config"])


def test_ruleset_model_cannot_bypass_live_definition_fence():
    """Shared semantic rule content must remain stable throughout execution."""

    workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.MUTABLE)
    ruleset = RulesetFactory(org=workflow.org, user=workflow.user)
    WorkflowStepFactory(workflow=workflow, ruleset=ruleset)
    ValidationRunFactory(workflow=workflow)
    ruleset.rules_text = '{"type": "string"}'

    with pytest.raises(WorkflowDefinitionInUseError):
        ruleset.save(update_fields=["rules_text"])


def test_error_message_uses_natural_pluralization():
    """The retry response should identify multiple users without awkward copy."""

    workflow = WorkflowFactory(history_policy=WorkflowHistoryPolicy.MUTABLE)
    submission = SubmissionFactory(workflow=workflow)
    ValidationRunFactory(workflow=workflow, submission=submission)
    ValidationRunFactory(workflow=workflow, submission=submission)

    with (
        pytest.raises(WorkflowDefinitionInUseError) as exc_info,
        guard_workflow_definition_mutation(workflow.pk),
    ):
        pass

    assert "2 validation runs" in exc_info.value.message
