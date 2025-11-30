"""
Utilities to compile and evaluate CEL expressions safely.

Uses cel-python for evaluation, adds simple limits and timeouts, and returns
structured results so engines can surface clear failures as findings.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import celpy

from simplevalidations.validations.constants import CEL_MAX_CONTEXT_SYMBOLS
from simplevalidations.validations.constants import CEL_MAX_EVAL_TIMEOUT_MS
from simplevalidations.validations.constants import CEL_MAX_EXPRESSION_CHARS


@dataclass(frozen=True)
class CelEvaluationResult:
    """Outcome of evaluating a CEL expression."""

    success: bool
    value: Any
    error: str = ""


@lru_cache(maxsize=256)
def _compile_expr(expr: str) -> celpy.Program:
    """
    Compile a CEL expression into a reusable program.
    Cached by expression string to avoid repeated compilation.
    """
    env = celpy.Environment()
    ast = env.compile(expr)
    return env.program(ast)


def evaluate_cel_expression(
    *,
    expression: str,
    context: dict[str, Any],
    timeout_ms: int | None = None,
) -> CelEvaluationResult:
    """
    Evaluate a CEL expression against a context, enforcing simple limits.
    Returns a CelEvaluationResult indicating success/value/error.
    """
    normalized = (expression or "").strip()
    if not normalized:
        return CelEvaluationResult(
            success=False, value=None, error="Empty CEL expression."
        )
    if len(normalized) > CEL_MAX_EXPRESSION_CHARS:
        return CelEvaluationResult(
            success=False,
            value=None,
            error="CEL expression is too long.",
        )
    if len(context) > CEL_MAX_CONTEXT_SYMBOLS:
        return CelEvaluationResult(
            success=False,
            value=None,
            error="CEL context is too large.",
        )

    def _evaluate() -> Any:
        program = _compile_expr(normalized)
        return program.evaluate(context)

    eval_timeout = (timeout_ms or CEL_MAX_EVAL_TIMEOUT_MS) / 1000.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_evaluate)
        try:
            value = future.result(timeout=eval_timeout)
            return CelEvaluationResult(success=True, value=value)
        except concurrent.futures.TimeoutError:
            return CelEvaluationResult(
                success=False,
                value=None,
                error="CEL evaluation timed out.",
            )
        except Exception as exc:
            return CelEvaluationResult(
                success=False,
                value=None,
                error=str(exc),
            )
