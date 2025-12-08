from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot.actions.forms import BaseWorkflowActionForm
    from validibot.actions.models import Action


ACTION_MODEL_REGISTRY: dict[str, type[Action]] = {}
ACTION_FORM_REGISTRY: dict[str, type[BaseWorkflowActionForm]] = {}


def register_action_model(action_type: str, model: type[Action]) -> None:
    ACTION_MODEL_REGISTRY[action_type] = model


def get_action_model(action_type: str) -> type[Action]:
    from validibot.actions.models import Action  # local import to avoid cycles

    return ACTION_MODEL_REGISTRY.get(action_type, Action)


def register_action_form(action_type: str, form: type[BaseWorkflowActionForm]) -> None:
    ACTION_FORM_REGISTRY[action_type] = form


def get_action_form(action_type: str) -> type[BaseWorkflowActionForm] | None:
    return ACTION_FORM_REGISTRY.get(action_type)
