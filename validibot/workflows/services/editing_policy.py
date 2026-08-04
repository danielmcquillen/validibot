"""Authoritative workflow editing-policy and live-definition mutation gates.

The HTML form is only a presentation layer. These services own the database
serialization that prevents first-run admission racing a policy transition and
prevents an executing run from observing a partly changed Mutable definition.
All normal semantic mutation paths should hold the workflow-row lock through
their write by using :func:`guard_workflow_definition_mutation`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from typing import Any

from django.core.exceptions import FieldDoesNotExist
from django.db import transaction
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from validibot.workflows.constants import WorkflowHistoryPolicy

if TYPE_CHECKING:
    from collections.abc import Iterator

    from validibot.users.models import User
    from validibot.workflows.models import Workflow


EDITING_POLICY_FIXED = "editing_policy_fixed"
WORKFLOW_DEFINITION_IN_USE = "workflow_definition_in_use"

# Workflow-row fields that can alter execution or the launch contract. This is
# intentionally separate from presentation/access/lifecycle fields so authors
# can still fix copy or deactivate a busy Mutable workflow before draining it.
WORKFLOW_DEFINITION_FIELDS = frozenset(
    {
        "allowed_file_types",
        "input_retention",
        "input_schema",
        "output_retention",
    },
)

# Fields shared by the step forms that are presentation-only for an existing
# step. Any unlisted field is conservatively semantic. A new step is always a
# semantic mutation because it changes the executable sequence even when its
# initial form contains only these common fields.
COSMETIC_WORKFLOW_STEP_FORM_FIELDS = frozenset(
    {
        "description",
        "display_schema",
        "name",
        "notes",
        "show_success_messages",
    },
)


class WorkflowEditingError(Exception):
    """Base class for stable, user-safe workflow mutation outcomes."""

    code = "workflow_editing_error"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class EditingPolicyFixedError(WorkflowEditingError):
    """Raised when history or an explicit lock fixes a workflow's policy."""

    code = EDITING_POLICY_FIXED


class WorkflowDefinitionInUseError(WorkflowEditingError):
    """Raised when unreleased runs fence a Mutable semantic mutation."""

    code = WORKFLOW_DEFINITION_IN_USE

    def __init__(self, *, active_run_count: int) -> None:
        self.active_run_count = active_run_count
        message = ngettext(
            "This workflow definition is currently being used by one "
            "validation run. Your changes have not been saved. Try again "
            "after that run finishes.",
            "This workflow definition is currently being used by "
            "%(count)s validation runs. Your changes have not been saved. "
            "Try again after those runs finish.",
            active_run_count,
        ) % {"count": active_run_count}
        super().__init__(message)


def editing_policy_is_fixed(workflow: Workflow) -> bool:
    """Return whether ordinary authors may still change editing policy."""

    return bool(workflow.is_locked or workflow.validation_runs.exists())


def validate_editing_policy_transition(
    *,
    workflow: Workflow,
    proposed_policy: str,
    actor: User | None,
) -> None:
    """Validate a policy transition against a freshly locked workflow row."""

    if proposed_policy == workflow.history_policy:
        return
    if getattr(actor, "is_superuser", False):
        return
    if editing_policy_is_fixed(workflow):
        raise EditingPolicyFixedError(
            str(
                _(
                    "This workflow gained validation history or was locked while "
                    "you were editing. Its editing policy is now fixed for this "
                    "version. Create a new workflow version to use a different "
                    "policy."
                ),
            ),
        )


def workflow_form_has_semantic_changes(
    *,
    workflow: Workflow | None,
    cleaned_data: dict[str, Any],
    current_values: dict[str, Any] | None = None,
) -> bool:
    """Return whether cleaned workflow settings alter definition semantics."""

    for field_name in WORKFLOW_DEFINITION_FIELDS:
        if field_name not in cleaned_data:
            continue
        current = (
            current_values.get(field_name)
            if current_values is not None
            else getattr(workflow, field_name, None)
        )
        proposed = cleaned_data[field_name]
        if field_name == "allowed_file_types":
            if set(current or []) != set(proposed or []):
                return True
        elif current != proposed:
            return True
    return False


def model_instance_has_semantic_changes(
    instance: Any,
    *,
    semantic_fields: tuple[str, ...],
    update_fields: (
        set[str] | frozenset[str] | list[str] | tuple[str, ...] | None
    ) = None,
) -> bool:
    """Compare one model instance with its persisted semantic field values."""

    if instance._state.adding or instance.pk is None:
        return True
    normalized_update_fields: set[str] = set()
    for field_name in update_fields or ():
        normalized_update_fields.add(field_name)
        try:
            model_field = instance._meta.get_field(field_name)
        except FieldDoesNotExist:
            continue
        normalized_update_fields.add(model_field.name)
        normalized_update_fields.add(model_field.attname)
    if normalized_update_fields and not normalized_update_fields.intersection(
        semantic_fields,
    ):
        return False
    try:
        original = type(instance).objects.get(pk=instance.pk)
    except type(instance).DoesNotExist:
        return True
    return any(
        getattr(instance, field_name, None) != getattr(original, field_name, None)
        for field_name in semantic_fields
    )


def workflow_step_form_has_semantic_changes(
    *,
    changed_fields: list[str] | tuple[str, ...] | set[str],
    is_new: bool,
) -> bool:
    """Classify a step-form save without duplicating validator field lists.

    Validator-specific forms evolve frequently. Treating every unknown field
    as semantic is fail-safe: a newly added execution option automatically
    participates in the definition-use fence until it is deliberately
    classified as cosmetic here.
    """

    return is_new or bool(
        set(changed_fields).difference(COSMETIC_WORKFLOW_STEP_FORM_FIELDS),
    )


def workflow_ids_for_ruleset(ruleset_id: int | None) -> list[int]:
    """Return sorted workflow IDs whose steps reference one ruleset."""

    if not ruleset_id:
        return []
    from validibot.workflows.models import WorkflowStep

    return list(
        WorkflowStep.objects.filter(ruleset_id=ruleset_id)
        .order_by("workflow_id")
        .values_list("workflow_id", flat=True)
        .distinct(),
    )


@contextmanager
def guard_workflow_definition_mutation(
    workflow_ids: int | list[int] | tuple[int, ...] | set[int],
    *,
    semantic_change: bool = True,
    include_versioned_definition_users: bool = False,
) -> Iterator[list[Workflow]]:
    """Lock workflows and reject a live Mutable semantic mutation atomically.

    Locks are acquired in primary-key order so multi-workflow shared resources
    cannot deadlock by visiting their dependants in different query orders.
    The caller's mutation occurs inside this context and therefore before the
    transaction releases the workflow locks.
    """

    from validibot.validations.models import ValidationRun
    from validibot.workflows.models import Workflow

    if isinstance(workflow_ids, int):
        normalized_ids = [workflow_ids]
    else:
        normalized_ids = sorted(set(workflow_ids))
    normalized_ids = [workflow_id for workflow_id in normalized_ids if workflow_id]

    with transaction.atomic():
        workflows = list(
            Workflow.objects.select_for_update()
            .filter(pk__in=normalized_ids)
            .order_by("pk"),
        )
        if semantic_change:
            fenced_ids = [
                workflow.pk
                for workflow in workflows
                if include_versioned_definition_users
                or workflow.history_policy == WorkflowHistoryPolicy.MUTABLE
            ]
            active_run_count = ValidationRun.objects.filter(
                workflow_id__in=fenced_ids,
                definition_released_at__isnull=True,
            ).count()
            if active_run_count:
                raise WorkflowDefinitionInUseError(
                    active_run_count=active_run_count,
                )
        yield workflows


@transaction.atomic
def change_editing_policy(
    *,
    workflow: Workflow,
    proposed_policy: str,
    actor: User | None,
) -> Workflow:
    """Validate and persist one editing-policy transition under a row lock."""

    from validibot.workflows.models import Workflow as WorkflowModel

    locked_workflow = WorkflowModel.objects.select_for_update().get(pk=workflow.pk)
    validate_editing_policy_transition(
        workflow=locked_workflow,
        proposed_policy=proposed_policy,
        actor=actor,
    )
    if proposed_policy != locked_workflow.history_policy:
        locked_workflow.history_policy = proposed_policy
        locked_workflow.save(update_fields=["history_policy", "modified"])
    return locked_workflow


__all__ = [
    "COSMETIC_WORKFLOW_STEP_FORM_FIELDS",
    "EDITING_POLICY_FIXED",
    "WORKFLOW_DEFINITION_FIELDS",
    "WORKFLOW_DEFINITION_IN_USE",
    "EditingPolicyFixedError",
    "WorkflowDefinitionInUseError",
    "change_editing_policy",
    "editing_policy_is_fixed",
    "guard_workflow_definition_mutation",
    "model_instance_has_semantic_changes",
    "validate_editing_policy_transition",
    "workflow_form_has_semantic_changes",
    "workflow_ids_for_ruleset",
    "workflow_step_form_has_semantic_changes",
]
