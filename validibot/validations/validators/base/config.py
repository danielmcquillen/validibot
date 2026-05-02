"""
Declarative configuration schema for Validibot system validators.

``ValidatorConfig`` is the **single source of truth** for each validator.
It carries all metadata the host system needs:

- **Identity and DB sync** — slug, name, catalog entries, etc.  Synced to
  the database by the ``sync_validators`` management command.
- **Validator class** — a dotted Python path (``validator_class``) resolved
  at startup so the runtime can instantiate the validator.
- **Step editor cards** — optional UI extensions (``step_editor_cards``)
  that inject custom cards into the workflow step detail page.

Every validator sub-package declares a ``ValidatorConfig`` instance in
its ``config.py`` module.  At
startup, ``register_validators()`` in ``validibot.validations.registrations``
discovers all configs and calls ``register_validator_config()`` for each,
populating both the **config registry** (metadata lookups) and the
**validator class registry** (runtime instantiation).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from validibot.validations.constants import ComputeTier

logger = logging.getLogger(__name__)


OFFICIAL_VALIDATOR_PLUGIN_PREFIXES = (
    "validibot",
    "validibot_pro",
    "validibot_enterprise",
)


class CatalogEntrySpec(BaseModel):
    """Specification for a single catalog entry (signal or derivation).

    Maps 1:1 to a ``SignalDefinition`` or ``Derivation`` row. The sync
    command uses these specs to create or update signals for a validator.
    """

    model_config = ConfigDict(frozen=True)

    slug: str
    label: str = ""
    entry_type: str
    run_stage: str = "output"
    data_type: str = "number"
    order: int = 0
    description: str = ""
    binding_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    is_required: bool = False
    source_kind: str = "payload_path"
    is_path_editable: bool = True


class StepEditorCardSpec(BaseModel):
    """Declares a custom card for the workflow step editor's right column.

    Validators use this to inject additional UI into the step detail page
    without scattering validator-specific template logic throughout the
    app.  The step detail view resolves these specs generically: it
    evaluates the ``condition``, instantiates the ``form_class`` (if
    provided), and renders ``template_name`` into the right column.

    Example::

        StepEditorCardSpec(
            slug="template-variables",
            label="Template Variables",
            template_name="workflows/partials/template_variables_card.html",
            form_class="validibot.workflows.forms.TemplateVariableAnnotationForm",
            view_class="validibot.workflows.views.WorkflowStepTemplateVariablesView",
            order=40,
            condition="validibot.workflows.views_helpers.step_has_template_variables",
        )
    """

    model_config = ConfigDict(frozen=True)

    # Unique identifier for this card (used for HTML id, HTMx targeting).
    slug: str
    # Display label shown in tab header.
    label: str
    # Django template path to render.
    template_name: str
    # Dotted path to a Form class (optional).  If provided, the card
    # renders an editable form.  Resolved via ``import_string()``.
    form_class: str = ""
    # Dotted path to a View class that handles GET/POST for this card.
    # If omitted, the card is rendered inline with no separate endpoint.
    view_class: str = ""
    # Order within the right column (lower = higher).
    order: int = 50
    # Dotted path to a ``func(step) -> bool`` condition.  If set, the
    # card only renders when this returns True.
    condition: str = ""


class ValidatorConfig(BaseModel):
    """Single source of truth for a system validator.

    Every validator declares a ``ValidatorConfig`` instance in a
    ``config.py`` module inside its sub-package.  At startup,
    ``register_validators()`` discovers all configs and registers each
    via ``register_validator_config()``.

    Example::

        # In validations/validators/therm/config.py
        config = ValidatorConfig(
            slug="therm-validator",
            name="THERM Validator",
            validation_type="THERM",
            validator_class=(
                "validibot.validations.validators.therm.validator.ThermValidator"
            ),
            ...
        )
    """

    model_config = ConfigDict(frozen=True)

    # --- Identity ---
    slug: str
    name: str
    description: str = ""
    validation_type: str
    provider: str = ""
    version: str = "1.0"
    order: int = 0
    has_processor: bool = False
    processor_name: str = ""
    is_system: bool = True
    # Whether this validator supports step-level assertions (Basic + CEL).
    # May evolve into a more granular field (e.g. list of assertion types)
    # if different validators need different assertion capabilities.
    supports_assertions: bool = False

    # --- Validator class ---
    # Dotted Python path to the BaseValidator subclass.  Resolved at
    # startup by ``register_validator_config()`` via ``import_string()``.
    validator_class: str = ""
    # The resolved Python class.  ``None`` in config declarations;
    # populated at registration time by ``register_validator_config()``.
    resolved_class: type[Any] | None = None

    # --- Output envelope class ---
    # Dotted Python path to the Pydantic model used to deserialize
    # the output.json returned by a container-based validator.  Only
    # relevant for advanced (container) validators — built-in validators
    # leave this empty.  Resolved at startup alongside ``validator_class``.
    output_envelope_class: str = ""
    # The resolved envelope Python class.  ``None`` unless the config
    # declares an ``output_envelope_class`` path.
    resolved_envelope_class: type[Any] | None = None

    # --- Container image (advanced/container-based validators) ---
    # The Docker image / Cloud Run job base name. Must match the
    # ``IMAGE_NAME`` declared in the corresponding validator backend's
    # ``__metadata__.py`` in the ``validibot-validator-backends`` repo.
    # When empty (the default), execution backends fall back to the
    # naming convention ``validibot-validator-backend-{slug}`` derived
    # from ``validation_type``.  Built-in validators leave this empty.
    image_name: str = ""

    # --- File handling ---
    supported_file_types: list[str] = Field(default_factory=list)
    supported_data_formats: list[str] = Field(default_factory=list)
    allowed_extensions: list[str] = Field(default_factory=list)
    resource_types: list[str] = Field(default_factory=list)

    # --- Compute ---
    compute_tier: str = ComputeTier.LOW

    # --- Trust ---
    # ADR-2026-04-27 Phase 5 Session C — first-party validator
    # backends (everything we ship today) ride the Phase 1 hardening
    # profile under TIER_1. Future user-added or partner-authored
    # backends declare TIER_2 to opt into the stricter sandbox
    # (egress allowlist, gVisor runtime when available, etc.).
    # Simple validators that don't dispatch to a container backend
    # leave this at TIER_1 by construction; the runner doesn't apply
    # tier-aware hardening for them.
    trust_tier: str = "TIER_1"

    # --- Display ---
    icon: str = "bi-journal-bookmark"
    card_image: str = "default_card_img_small.png"

    # --- Catalog ---
    catalog_entries: list[CatalogEntrySpec] = Field(default_factory=list)

    # --- Step editor UI extensions ---
    # Custom cards rendered in the step detail page's right column.
    step_editor_cards: list[StepEditorCardSpec] = Field(default_factory=list)


def _get_allowed_validator_plugin_prefixes() -> tuple[str, ...]:
    """Return the allowlisted module prefixes for validator plugins."""

    configured = getattr(
        settings,
        "VALIDIBOT_ALLOWED_VALIDATOR_PLUGIN_PREFIXES",
        OFFICIAL_VALIDATOR_PLUGIN_PREFIXES,
    )
    return tuple(configured)


def _provider_is_allowed(provider: str) -> bool:
    """Check whether a validator provider module is allowlisted."""

    allowed_prefixes = _get_allowed_validator_plugin_prefixes()
    return any(
        provider == prefix or provider.startswith(f"{prefix}.")
        for prefix in allowed_prefixes
    )


def _ensure_allowed_provider(provider: str) -> None:
    """Reject validator configs from unexpected module namespaces."""

    if not provider:
        return
    if _provider_is_allowed(provider):
        return
    allowed_prefixes = ", ".join(_get_allowed_validator_plugin_prefixes())
    raise ImproperlyConfigured(
        "Validator plugin provider "
        f"'{provider}' is not allowed. Set "
        "'VALIDIBOT_ALLOWED_VALIDATOR_PLUGIN_PREFIXES' to include it if this "
        f"is intentional. Current allowlist: {allowed_prefixes}",
    )


def _infer_config_provider(
    config: ValidatorConfig,
    *,
    fallback_provider: str = "",
) -> str:
    """Resolve the provider module name for a validator config."""

    if config.provider:
        return config.provider
    if fallback_provider:
        return fallback_provider
    if config.validator_class:
        return config.validator_class.rsplit(".", maxsplit=1)[0]
    if config.output_envelope_class:
        return config.output_envelope_class.rsplit(".", maxsplit=1)[0]
    return ""


def discover_configs() -> list[ValidatorConfig]:
    """Scan validator sub-packages for config modules.

    Walks ``validibot.validations.validators/`` and imports any
    sub-package containing a ``config`` module with a ``config``
    attribute that is a ``ValidatorConfig`` instance.

    Sub-packages without a config module (e.g. ``base``, ``ai``,
    ``basic``) are silently skipped.

    Returns:
        List of discovered ``ValidatorConfig`` instances, sorted by
        ``order`` then ``name``.
    """
    import validibot.validations.validators as validators_pkg

    configs: list[ValidatorConfig] = []

    for _importer, modname, ispkg in pkgutil.iter_modules(validators_pkg.__path__):
        if not ispkg or modname == "base":
            # Skip single-file validators and the base infrastructure package
            continue

        config_module_name = f"validibot.validations.validators.{modname}.config"
        try:
            mod = importlib.import_module(config_module_name)
        except ModuleNotFoundError:
            # This sub-package doesn't have a config module — that's fine.
            continue

        config_attr = getattr(mod, "config", None)
        if isinstance(config_attr, ValidatorConfig):
            provider = _infer_config_provider(
                config_attr,
                fallback_provider=config_module_name,
            )
            _ensure_allowed_provider(provider)
            configs.append(
                config_attr.model_copy(
                    update={"provider": provider},
                ),
            )
        else:
            logger.warning(
                "Module %s exists but has no ValidatorConfig 'config' attribute",
                config_module_name,
            )

    configs.sort(key=lambda c: (c.order, c.name))
    return configs


# ---------------------------------------------------------------------------
# Unified Registry
#
# Single registry keyed by validation_type, populated at startup by
# register_validators() in validibot.validations.registrations.
#
# Each ValidatorConfig stored here carries both metadata (slug, catalog
# entries, etc.) and resolved class references (resolved_class,
# resolved_envelope_class) set at registration time.
#
# Consumers use:
#   get_config() / get_all_configs() — metadata lookups
#   get_validator_class() — resolved validator class for instantiation
#   get_output_envelope_class() — resolved envelope class for container output
# ---------------------------------------------------------------------------

_CONFIG_REGISTRY: dict[str, ValidatorConfig] = {}


def register_validator_config(config: ValidatorConfig) -> None:
    """Register a validator config, resolving its class references.

    External packages call this from their ``AppConfig.ready()`` to register
    validators, following the same pattern as ``register_action_descriptor()``
    for actions.

    The provider module is checked against the allowlist before the config
    becomes visible to the rest of the system. The ``validator_class`` and
    ``output_envelope_class`` dotted paths are resolved via
    ``import_string()`` and stored on the config as ``resolved_class`` and
    ``resolved_envelope_class``.

    Raises:
        ImproperlyConfigured: If the provider is not in the allowlist.
        ValueError: If ``validation_type`` is already registered.
        ImportError: If ``validator_class`` or ``output_envelope_class``
            cannot be resolved.
    """
    from django.utils.module_loading import import_string

    provider = _infer_config_provider(config)
    _ensure_allowed_provider(provider)

    cfg = (
        config if config.provider else config.model_copy(update={"provider": provider})
    )

    if cfg.validation_type in _CONFIG_REGISTRY:
        msg = (
            f"Duplicate config registration for validation_type "
            f"'{cfg.validation_type}': {cfg.slug} conflicts with "
            f"{_CONFIG_REGISTRY[cfg.validation_type].slug}"
        )
        raise ValueError(msg)

    updates: dict[str, Any] = {}

    if cfg.validator_class:
        try:
            cls = import_string(cfg.validator_class)
        except (ImportError, AttributeError) as exc:
            raise ImportError(
                f"Cannot import validator class '{cfg.validator_class}' "
                f"declared in config '{cfg.slug}' "
                f"(validation_type='{cfg.validation_type}'): {exc}"
            ) from exc
        cls.validation_type = cfg.validation_type
        updates["resolved_class"] = cls

    if cfg.output_envelope_class:
        try:
            envelope_cls = import_string(cfg.output_envelope_class)
        except (ImportError, AttributeError) as exc:
            raise ImportError(
                f"Cannot import output envelope class "
                f"'{cfg.output_envelope_class}' declared in config "
                f"'{cfg.slug}' "
                f"(validation_type='{cfg.validation_type}'): {exc}"
            ) from exc
        updates["resolved_envelope_class"] = envelope_cls

    if updates:
        cfg = cfg.model_copy(update=updates)

    _CONFIG_REGISTRY[cfg.validation_type] = cfg


def get_config(validation_type: str) -> ValidatorConfig | None:
    """Look up the config for a given validation type.

    Returns ``None`` if no config is registered (e.g. a dynamically
    created custom validator). Callers should apply their own defaults.
    """
    return _CONFIG_REGISTRY.get(validation_type)


def get_all_configs() -> list[ValidatorConfig]:
    """Return all registered configs, sorted by ``(order, name)``."""
    configs = list(_CONFIG_REGISTRY.values())
    configs.sort(key=lambda c: (c.order, c.name))
    return configs


def get_validator_class(vtype: str) -> type[Any]:
    """Retrieve the resolved validator class for a given validation type.

    Raises ``KeyError`` if not registered or no class was resolved.
    """
    key = getattr(vtype, "value", None) or str(vtype)
    cfg = _CONFIG_REGISTRY.get(str(key))
    if cfg is None or cfg.resolved_class is None:
        raise KeyError(key)
    return cfg.resolved_class


def get_output_envelope_class(vtype: str) -> type[Any] | None:
    """Retrieve the resolved output envelope class for a validation type.

    Returns ``None`` if no envelope class is registered (e.g. built-in
    validators that don't use container envelopes).
    """
    key = getattr(vtype, "value", None) or str(vtype)
    cfg = _CONFIG_REGISTRY.get(str(key))
    if cfg is None:
        return None
    return cfg.resolved_envelope_class
