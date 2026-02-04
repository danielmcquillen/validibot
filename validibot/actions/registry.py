from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from validibot.actions.forms import BaseWorkflowActionForm
    from validibot.actions.models import Action


ACTION_MODEL_REGISTRY: dict[str, type[Action]] = {}
ACTION_FORM_REGISTRY: dict[str, type[BaseWorkflowActionForm]] = {}
ACTION_HANDLER_REGISTRY: dict[str, type[Any]] = {}  # Map type_id -> Handler Class


def register_action_model(action_type: str, model: type[Action]) -> None:
    ACTION_MODEL_REGISTRY[action_type] = model


def get_action_model(action_type: str) -> type[Action]:
    from validibot.actions.models import Action  # local import to avoid cycles

    return ACTION_MODEL_REGISTRY.get(action_type, Action)


def register_action_form(action_type: str, form: type[BaseWorkflowActionForm]) -> None:
    ACTION_FORM_REGISTRY[action_type] = form


def get_action_form(action_type: str) -> type[BaseWorkflowActionForm] | None:
    return ACTION_FORM_REGISTRY.get(action_type)


def register_action_handler(action_type: str, handler: type[Any]) -> None:
    """Register a execution handler for a specific action type."""
    ACTION_HANDLER_REGISTRY[action_type] = handler


def get_action_handler(action_type: str) -> type[Any] | None:
    """Retrieve the execution handler for a specific action type."""
    return ACTION_HANDLER_REGISTRY.get(action_type)
