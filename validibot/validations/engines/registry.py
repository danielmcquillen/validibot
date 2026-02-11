"""
Registry for validator engines.

What's a validator engine?

A class that subclasses BaseValidatorEngine and implements the validate() method.
This is what does the actual validation work in a given validation step.

These engines are mapped to a ValidationType (string value) and can be looked up
via that type.

Any new validator engine must be registered via the @register_engine decorator.

For example:

    from validibot.validations.engines.base import BaseValidatorEngine
    from validibot.validations.engines.registry import register_engine

    @register_engine(ValidationType.SOME_CRAZY_TYPE)
    class SomeCrazyValidatorEngine(BaseValidatorEngine):
        ...


"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from validibot.validations.constants import ValidationType
    from validibot.validations.engines.base import BaseValidatorEngine

# Global registry mapping ValidationType (string value) -> Validator class
_ENGINE_REGISTRY: dict[str, type[BaseValidatorEngine]] = {}


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
        _ENGINE_REGISTRY[str(key)] = cls
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
    return _ENGINE_REGISTRY[str(key)]
