"""Validator config registrations for the community app.

This module registers all validators that ship with community Validibot.
Every validator sub-package under ``validations/validators/`` with a
``config.py`` module is auto-discovered and registered.

Commercial packages register their own validator configs from their own
AppConfig.ready() methods, following the same pattern via
``register_validator_config()``.
"""

from validibot.validations.validators.base.config import _CONFIG_REGISTRY
from validibot.validations.validators.base.config import discover_configs
from validibot.validations.validators.base.config import register_validator_config


def register_validators() -> None:
    """Register all community validator configs.

    Auto-discovers every validator sub-package with a ``config.py``
    module and registers each via ``register_validator_config()``.

    Idempotent: skips if the registry is already populated (handles
    Django's autoreloader calling ``ready()`` twice).
    """
    if _CONFIG_REGISTRY:
        return

    for config in discover_configs():
        register_validator_config(config)
