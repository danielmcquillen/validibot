from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from validibot.validations.constants import ValidationType
    from validibot.validations.models import Validator
    from validibot.validations.providers.base import BaseValidationProvider

_PROVIDER_REGISTRY: dict[str, type[BaseValidationProvider]] = {}

if TYPE_CHECKING:
    from collections.abc import Callable


def register_provider(
    validation_type: ValidationType,
) -> Callable[[type[BaseValidationProvider]], type[BaseValidationProvider]]:
    """
    Decorator used by providers to register themselves for a validation type.
    """

    def _inner(cls: type[BaseValidationProvider]) -> type[BaseValidationProvider]:
        key = getattr(validation_type, "value", str(validation_type))
        _PROVIDER_REGISTRY[key] = cls
        cls.validation_type = validation_type  # type: ignore[attr-defined]
        return cls

    return _inner


def get_provider_class(validation_type: ValidationType):
    key = getattr(validation_type, "value", str(validation_type))
    return _PROVIDER_REGISTRY.get(key)


def get_provider_for_validator(
    validator: Validator,
) -> BaseValidationProvider | None:
    provider_class = get_provider_class(validator.validation_type)
    if provider_class is None:
        return None
    return provider_class(validator=validator)
