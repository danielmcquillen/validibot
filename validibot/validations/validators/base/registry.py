"""
Validator class and output envelope registries.

Maps ``ValidationType`` (string value) to:

- **Validator class** — for runtime instantiation of validators.
- **Output envelope class** — for deserializing the ``output.json``
  returned by container-based (advanced) validators.

Both registries are **populated by** ``config.populate_registry()`` at
startup.  It reads the ``validator_class`` and ``output_envelope_class``
dotted paths from each ``ValidatorConfig`` and resolves them via
``import_string()``.  There is no separate registration step; the
``ValidatorConfig`` is the single source of truth for metadata, class
binding, and envelope binding.

Usage::

    from validibot.validations.validators.base import registry

    # Validator class lookup
    cls = registry.get("ENERGYPLUS")

    # Output envelope class lookup
    envelope_cls = registry.get_output_envelope_class("ENERGYPLUS")
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel

    from validibot.validations.validators.base.base import BaseValidator

# Global registry mapping ValidationType (string value) -> Validator class.
# Populated by config.populate_registry() at startup.
_VALIDATOR_REGISTRY: dict[str, type[BaseValidator]] = {}

# Global registry mapping ValidationType (string value) -> output envelope
# Pydantic model class.  Only advanced (container) validators have entries.
# Populated by config.populate_registry() at startup.
_ENVELOPE_REGISTRY: dict[str, type[BaseModel]] = {}


def get(vtype: str) -> type[BaseValidator]:
    """Retrieve the validator class for a given validation type.

    Raises ``KeyError`` if not registered.
    """
    key = getattr(vtype, "value", None) or str(vtype)
    return _VALIDATOR_REGISTRY[str(key)]


def get_output_envelope_class(vtype: str) -> type[BaseModel] | None:
    """Retrieve the output envelope class for a given validation type.

    Returns ``None`` if no envelope class is registered (e.g. built-in
    validators that don't use container envelopes).
    """
    key = getattr(vtype, "value", None) or str(vtype)
    return _ENVELOPE_REGISTRY.get(str(key))
