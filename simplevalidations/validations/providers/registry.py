from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover
    from simplevalidations.validations.providers.base import BaseValidationProvider
    from simplevalidations.validations.models import Validator
    from simplevalidations.validations.constants import ValidationType

_PROVIDER_REGISTRY: dict[str, type["BaseValidationProvider"]] = {}


def register_provider(
    validation_type: "ValidationType",
) -> Callable[[type["BaseValidationProvider"]], type["BaseValidationProvider"]]:
    """
    Decorator used by providers to register themselves for a validation type.
    """

    def _inner(cls: type["BaseValidationProvider"]) -> type["BaseValidationProvider"]:
        key = getattr(validation_type, "value", str(validation_type))
        _PROVIDER_REGISTRY[key] = cls
        cls.validation_type = validation_type  # type: ignore[attr-defined]
        return cls

    return _inner


def get_provider_class(validation_type: "ValidationType"):
    key = getattr(validation_type, "value", str(validation_type))
    return _PROVIDER_REGISTRY.get(key)


def get_provider_for_validator(validator: "Validator") -> "BaseValidationProvider | None":
    ProviderClass = get_provider_class(validator.validation_type)
    if ProviderClass is None:
        return None
    return ProviderClass(validator=validator)
