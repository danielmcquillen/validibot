from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from roscoe.validations.constants import ValidationType
    from roscoe.validations.engines.base import BaseValidatorEngine

# Global registry mapping ValidationType (string value) -> Validator class
_REGISTRY: dict[str, type[BaseValidatorEngine]] = {}


def register_engine(
    vtype: ValidationType,
) -> Callable[
    [type[BaseValidatorEngine]],
    type[BaseValidatorEngine],
]:
    """
    Decorator to register a validator class for a given ValidationType.
    Usage:
      @register(ValidationType.JSON_SCHEMA)
      class JsonSchemaValidator(BaseValidator): ...
    """

    def _inner(cls: type[BaseValidatorEngine]) -> type[BaseValidatorEngine]:
        key = getattr(vtype, "value", None) or str(vtype)
        _REGISTRY[str(key)] = cls
        # Store canonical value for reference on the class
        cls.validation_type = vtype  # type: ignore[attr-defined]
        return cls

    return _inner


def get(vtype: ValidationType | str) -> type[BaseValidatorEngine]:
    """
    Retrieve the validator class for a given ValidationType.
    Raises KeyError if not registered.
    """
    key = getattr(vtype, "value", None) or str(vtype)
    return _REGISTRY[str(key)]
