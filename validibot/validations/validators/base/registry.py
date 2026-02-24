"""
Registry for validators.

A validator is a class that subclasses BaseValidator and implements the
validate() method. This is what does the actual validation work in a given
validation step.

Validators are mapped to a ValidationType (string value) and can be looked
up via that type.

Any new validator must be registered via the @register_validator decorator.

For example::

    from validibot.validations.validators.base import BaseValidator
    from validibot.validations.validators.base import register_validator

    @register_validator(ValidationType.SOME_TYPE)
    class SomeValidator(BaseValidator):
        ...
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from validibot.validations.constants import ValidationType
    from validibot.validations.validators.base.base import BaseValidator

# Global registry mapping ValidationType (string value) -> Validator class
_VALIDATOR_REGISTRY: dict[str, type[BaseValidator]] = {}


def register_validator(
    vtype: ValidationType,
) -> Callable[
    [type[BaseValidator]],
    type[BaseValidator],
]:
    """
    Decorator to register a validator class for a given ValidationType.

    Usage::

        @register_validator(ValidationType.JSON_SCHEMA)
        class JsonSchemaValidator(BaseValidator): ...
    """

    def _inner(cls: type[BaseValidator]) -> type[BaseValidator]:
        key = getattr(vtype, "value", None) or str(vtype)
        _VALIDATOR_REGISTRY[str(key)] = cls
        # Store canonical value for reference on the class
        cls.validation_type = vtype  # type: ignore[attr-defined]
        return cls

    return _inner


def get(vtype: ValidationType | str) -> type[BaseValidator]:
    """
    Retrieve the validator class for a given ValidationType.
    Raises KeyError if not registered.
    """
    key = getattr(vtype, "value", None) or str(vtype)
    return _VALIDATOR_REGISTRY[str(key)]
