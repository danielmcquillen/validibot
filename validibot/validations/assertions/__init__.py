"""
Assertion evaluation infrastructure.

This package provides a Strategy + Registry pattern for evaluating different
types of ruleset assertions (BASIC, CEL, future types) in a unified way.
"""

from validibot.validations.assertions.evaluators.base import AssertionContext
from validibot.validations.assertions.evaluators.base import AssertionEvaluator
from validibot.validations.assertions.evaluators.registry import get_evaluator
from validibot.validations.assertions.evaluators.registry import register_evaluator

__all__ = [
    "AssertionContext",
    "AssertionEvaluator",
    "get_evaluator",
    "register_evaluator",
]
