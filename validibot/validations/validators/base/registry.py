"""
Validator class registry.

Maps ``ValidationType`` (string value) to validator class for runtime
instantiation.  Consumers call ``get()`` to retrieve the class for a
given validation type.

This registry is **populated by** ``config.populate_registry()`` at
startup — it reads the ``validator_class`` dotted path from each
``ValidatorConfig`` and resolves it via ``import_string()``.  There is
no separate registration step; the ``ValidatorConfig`` is the single
source of truth for both metadata and class binding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot.validations.validators.base.base import BaseValidator

# Global registry mapping ValidationType (string value) -> Validator class.
# Populated by config.populate_registry() at startup.
_VALIDATOR_REGISTRY: dict[str, type[BaseValidator]] = {}


def get(vtype: str) -> type[BaseValidator]:
    """Retrieve the validator class for a given validation type.

    Raises ``KeyError`` if not registered.
    """
    key = getattr(vtype, "value", None) or str(vtype)
    return _VALIDATOR_REGISTRY[str(key)]
