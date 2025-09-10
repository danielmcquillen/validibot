from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from roscoe.validations.constants import ValidationType
    from roscoe.validations.engines.base import BaseValidatorEngine

# Global registry mapping ValidationType -> Validator class
_REGISTRY: dict[ValidationType, type[BaseValidatorEngine]] = {}


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
        _REGISTRY[vtype] = cls
        cls.validation_type = vtype
        return cls

    return _inner


def get(vtype: ValidationType) -> type[BaseValidatorEngine]:
    """
    Retrieve the validator class for a given ValidationType.
    Raises KeyError if not registered.
    """
    return _REGISTRY[vtype]
