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

Every validator — whether a sub-package with its own ``config.py`` or a
single-file built-in — declares a ``ValidatorConfig`` instance.  The
``populate_registry()`` function, called once from
``ValidationsConfig.ready()``, discovers all configs and populates both
the **config registry** (metadata lookups) and the **validator class
registry** (runtime instantiation).

This replaces the previous two-registry approach where configs and
classes were registered separately.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from validibot.validations.constants import ComputeTier

logger = logging.getLogger(__name__)


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

    Every validator declares a ``ValidatorConfig`` instance — either in a
    ``config.py`` module inside its sub-package, or in
    ``builtin_configs.py`` for single-file validators.  At startup,
    ``populate_registry()`` discovers all configs and populates both the
    config registry (metadata) and the validator class registry (runtime).

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
    # startup by ``populate_registry()`` via ``import_string()``.
    validator_class: str = ""

    # --- Output envelope class ---
    # Dotted Python path to the Pydantic model used to deserialize
    # the output.json returned by a container-based validator.  Only
    # relevant for advanced (container) validators — built-in validators
    # leave this empty.  Resolved at startup alongside ``validator_class``
    # and stored in ``registry._ENVELOPE_REGISTRY`` for O(1) lookups.
    output_envelope_class: str = ""

    # --- File handling ---
    supported_file_types: list[str] = Field(default_factory=list)
    supported_data_formats: list[str] = Field(default_factory=list)
    allowed_extensions: list[str] = Field(default_factory=list)
    resource_types: list[str] = Field(default_factory=list)

    # --- Compute ---
    compute_tier: str = ComputeTier.LOW

    # --- Display ---
    icon: str = "bi-journal-bookmark"
    card_image: str = "default_card_img_small.png"

    # --- Catalog ---
    catalog_entries: list[CatalogEntrySpec] = Field(default_factory=list)

    # --- Step editor UI extensions ---
    # Custom cards rendered in the step detail page's right column.
    step_editor_cards: list[StepEditorCardSpec] = Field(default_factory=list)


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
            configs.append(config_attr)
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
# Populated once at startup by populate_registry(), keyed by validation_type.
#
# Two registries are populated from a single pass over all ValidatorConfig
# instances:
#
#   _CONFIG_REGISTRY — ValidatorConfig metadata (slug, catalog entries, etc.)
#   _VALIDATOR_REGISTRY — validator class references (for runtime instantiation)
#
# The validator class registry lives in registry.py but is populated here.
# Consumers use get_config() / get_all_configs() for metadata lookups and
# registry.get() for validator class lookups.
# ---------------------------------------------------------------------------

_CONFIG_REGISTRY: dict[str, ValidatorConfig] = {}


def populate_registry() -> None:
    """Discover all configs and populate the config, class, and envelope registries.

    Called once from ``ValidationsConfig.ready()``.  Pulls configs from:

    1. ``discover_configs()`` — package-based validators with ``config.py``
    2. ``BUILTIN_CONFIGS`` — single-file built-in validators

    For each config, resolves dotted paths via ``import_string()`` and
    populates:

    - ``registry._VALIDATOR_REGISTRY`` — from ``validator_class``
    - ``registry._ENVELOPE_REGISTRY`` — from ``output_envelope_class``

    Idempotent: skips if the registry is already populated (handles
    Django's autoreloader calling ``ready()`` twice).
    """
    if _CONFIG_REGISTRY:
        return

    from django.utils.module_loading import import_string

    from validibot.validations.validators.base.builtin_configs import BUILTIN_CONFIGS
    from validibot.validations.validators.base.registry import _ENVELOPE_REGISTRY
    from validibot.validations.validators.base.registry import _VALIDATOR_REGISTRY

    all_configs = list(discover_configs()) + list(BUILTIN_CONFIGS)

    for cfg in all_configs:
        if cfg.validation_type in _CONFIG_REGISTRY:
            msg = (
                f"Duplicate config registration for validation_type "
                f"'{cfg.validation_type}': {cfg.slug} conflicts with "
                f"{_CONFIG_REGISTRY[cfg.validation_type].slug}"
            )
            raise ValueError(msg)
        _CONFIG_REGISTRY[cfg.validation_type] = cfg

        if cfg.validator_class:
            # Wrap import_string() with context so a typo in any of the
            # 7+ validator configs produces an error message that names
            # the offending config — not just the missing module/attribute.
            try:
                cls = import_string(cfg.validator_class)
            except (ImportError, AttributeError) as exc:
                raise ImportError(
                    f"Cannot import validator class '{cfg.validator_class}' "
                    f"declared in config '{cfg.slug}' "
                    f"(validation_type='{cfg.validation_type}'): {exc}"
                ) from exc
            _VALIDATOR_REGISTRY[cfg.validation_type] = cls
            cls.validation_type = cfg.validation_type

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
            _ENVELOPE_REGISTRY[cfg.validation_type] = envelope_cls


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
