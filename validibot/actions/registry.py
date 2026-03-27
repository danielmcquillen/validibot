from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from validibot.actions.forms import BaseWorkflowActionForm
    from validibot.actions.models import Action


OFFICIAL_ACTION_PLUGIN_PREFIXES = (
    "validibot",
    "validibot_pro",
    "validibot_enterprise",
)


@dataclass(frozen=True)
class ActionDescriptor:
    """Metadata and runtime bindings for a workflow action plugin.

    Community code owns this shared contract. Individual Django apps
    register descriptors for the actions they provide. The descriptor
    drives three things:

    1. Seeding or syncing ``ActionDefinition`` rows
    2. Runtime lookup of the concrete model, form, and handler
    3. Guarding plugin registration to known package namespaces
    """

    slug: str
    name: str
    description: str
    icon: str
    action_category: str
    type: str
    model: type[Action]
    form: type[BaseWorkflowActionForm]
    handler: type[Any]
    required_commercial_feature: str = ""
    provider: str = ""


ACTION_MODEL_REGISTRY: dict[str, type[Action]] = {}
ACTION_FORM_REGISTRY: dict[str, type[BaseWorkflowActionForm]] = {}
ACTION_HANDLER_REGISTRY: dict[str, type[Any]] = {}  # Map type_id -> Handler Class
ACTION_DESCRIPTOR_REGISTRY: dict[str, ActionDescriptor] = {}


def _get_allowed_action_plugin_prefixes() -> tuple[str, ...]:
    """Return the allowed module prefixes for action plugins.

    This keeps action loading explicit and conservative. Self-host users
    can widen the allowlist in settings if they intentionally want to load
    third-party action plugins.
    """

    configured = getattr(
        settings,
        "VALIDIBOT_ALLOWED_ACTION_PLUGIN_PREFIXES",
        OFFICIAL_ACTION_PLUGIN_PREFIXES,
    )
    return tuple(configured)


def _provider_is_allowed(provider: str) -> bool:
    """Check whether a plugin provider module is allowlisted."""

    allowed_prefixes = _get_allowed_action_plugin_prefixes()
    return any(
        provider == prefix or provider.startswith(f"{prefix}.")
        for prefix in allowed_prefixes
    )


def _ensure_allowed_provider(provider: str) -> None:
    """Reject action plugin registrations from unexpected module namespaces."""

    if not provider:
        return
    if _provider_is_allowed(provider):
        return
    allowed_prefixes = ", ".join(_get_allowed_action_plugin_prefixes())
    raise ImproperlyConfigured(
        "Action plugin provider "
        f"'{provider}' is not allowed. Set "
        "'VALIDIBOT_ALLOWED_ACTION_PLUGIN_PREFIXES' to include it if this "
        f"is intentional. Current allowlist: {allowed_prefixes}",
    )


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


def register_action_descriptor(descriptor: ActionDescriptor) -> None:
    """Register a complete action plugin descriptor.

    The provider module is checked against the allowlist before the
    descriptor becomes visible to the rest of the system.
    """

    provider = descriptor.provider or descriptor.model.__module__
    _ensure_allowed_provider(provider)
    ACTION_DESCRIPTOR_REGISTRY[descriptor.type] = descriptor
    register_action_model(descriptor.type, descriptor.model)
    register_action_form(descriptor.type, descriptor.form)
    register_action_handler(descriptor.type, descriptor.handler)


def get_action_descriptor(action_type: str) -> ActionDescriptor | None:
    """Return the registered descriptor for an action type, if any."""

    return ACTION_DESCRIPTOR_REGISTRY.get(action_type)


def get_action_descriptors() -> list[ActionDescriptor]:
    """Return all registered action descriptors in registration order."""

    return list(ACTION_DESCRIPTOR_REGISTRY.values())
