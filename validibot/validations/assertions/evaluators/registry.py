"""
Registry for assertion evaluators.

Maps assertion type strings to evaluator instances using a decorator pattern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

    from validibot.validations.assertions.evaluators.base import AssertionEvaluator

T = TypeVar("T")

_EVALUATOR_REGISTRY: dict[str, AssertionEvaluator] = {}


def register_evaluator(assertion_type: str) -> Callable[[type[T]], type[T]]:
    """
    Decorator to register an evaluator class for an assertion type.

    Usage:
        @register_evaluator(AssertionType.BASIC)
        class BasicAssertionEvaluator:
            def evaluate(self, *, assertion, payload, context):
                ...

    Args:
        assertion_type: The assertion type string (e.g., "basic", "cel_expr").

    Returns:
        Decorator that registers the class and returns it unchanged.
    """

    def decorator(cls: type[T]) -> type[T]:
        _EVALUATOR_REGISTRY[assertion_type] = cls()
        return cls

    return decorator


def get_evaluator(assertion_type: str) -> AssertionEvaluator | None:
    """
    Get the evaluator for a given assertion type.

    Args:
        assertion_type: The assertion type string.

    Returns:
        The registered evaluator instance, or None if not registered.
    """
    return _EVALUATOR_REGISTRY.get(assertion_type)


def get_registered_types() -> list[str]:
    """Return list of registered assertion type strings."""
    return list(_EVALUATOR_REGISTRY.keys())
