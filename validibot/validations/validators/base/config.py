"""
Declarative configuration schema for Validibot system validators.

Every validator package that needs database synchronization includes a
``config.py`` module with a module-level ``config`` instance of
``ValidatorConfig``. The config carries all metadata the host system
needs to register the validator in the database and populate its
catalog entries.

The ``discover_configs()`` function scans validator sub-packages for
these config modules and returns the list of configs for the
``sync_validators`` management command.

This follows the Django convention of using config modules for
application-level configuration (cf. AppConfig, django-appconf)
and uses Pydantic for schema validation.
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

    Maps 1:1 to a ``ValidatorCatalogEntry`` row. The sync command uses
    these specs to create or update catalog entries for a validator.
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


class ValidatorConfig(BaseModel):
    """Declarative metadata for a system validator.

    Each validator package that needs its metadata synced to the database
    exposes a module-level ``config`` instance of this class in its
    ``config.py`` module. The ``discover_configs()`` function collects
    these and the ``sync_validators`` management command writes them to
    the database.

    Example::

        # In validations/validators/therm/config.py
        from validibot.validations.validators.base.config import (
            CatalogEntrySpec,
            ValidatorConfig,
        )

        config = ValidatorConfig(
            slug="therm-validator",
            name="THERM Validator",
            validation_type="THERM",
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
# Config Registry
#
# Populated once at startup by populate_registry(), keyed by validation_type.
# Consumers use get_config() for single lookups and get_all_configs() for
# the full list. Functions that read from this registry handle None gracefully
# (not every validation_type has a config — e.g. dynamically created custom
# validators).
# ---------------------------------------------------------------------------

_CONFIG_REGISTRY: dict[str, ValidatorConfig] = {}


def populate_registry() -> None:
    """Discover all configs and populate the global registry.

    Called once from ``ValidationsConfig.ready()``. Pulls configs from:

    1. ``discover_configs()`` — package-based validators with ``config.py``
    2. ``BUILTIN_CONFIGS`` — single-file built-in validators

    Idempotent: skips if the registry is already populated (handles
    Django's autoreloader calling ``ready()`` twice).
    """
    if _CONFIG_REGISTRY:
        return

    from validibot.validations.validators.base.builtin_configs import BUILTIN_CONFIGS

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
