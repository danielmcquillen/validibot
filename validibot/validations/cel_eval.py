"""
Utilities to compile and evaluate CEL expressions safely.

Uses cel-python for evaluation, adds simple limits and timeouts, and returns
structured results so validators can surface clear failures as findings.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import celpy
from lark import Token
from lark import Tree

from validibot.validations.constants import CEL_MAX_CONTEXT_DEPTH
from validibot.validations.constants import CEL_MAX_CONTEXT_SYMBOLS
from validibot.validations.constants import CEL_MAX_CONTEXT_TOTAL_SYMBOLS
from validibot.validations.constants import CEL_MAX_EVAL_TIMEOUT_MS
from validibot.validations.constants import CEL_MAX_EXPRESSION_CHARS
from validibot.validations.constants import CEL_MAX_MACRO_COUNT
from validibot.validations.constants import CEL_MAX_MACRO_NESTING


@dataclass(frozen=True)
class CelEvaluationResult:
    """Outcome of evaluating a CEL expression."""

    success: bool
    value: Any
    error: str = ""


class _CelContextShapeError(ValueError):
    """Raised internally when the CEL context exceeds depth/symbol limits.

    Caught by :func:`evaluate_cel_expression` and surfaced as a
    ``CelEvaluationResult(success=False, ...)``. Defined as a dedicated
    subclass so the caller can distinguish shape violations from
    generic ValueError raised by celpy.
    """


class _CelExpressionShapeError(ValueError):
    """Raised when the compiled CEL expression AST violates shape limits.

    Separate from :class:`_CelContextShapeError` — that one guards the
    data side (submitter-controlled), this one guards the expression
    side (author-controlled). Different attackers, different fixes.
    """


# CEL macros per the cel-spec: ``all``, ``exists``, ``exists_one``,
# ``map``, ``filter``. celpy also exposes ``reduce`` as an extension.
# These are the only CEL constructs that can produce exponential
# evaluation cost, per cel-spec langdef.md.
_CEL_MACRO_METHODS: frozenset[str] = frozenset(
    {"all", "exists", "exists_one", "map", "filter", "reduce"},
)

# Lark AST child positions for a ``member_dot_arg`` node.
# celpy parses ``receiver.method(args)`` into three children:
#   children[0] = ``member`` subtree (the receiver)
#   children[1] = ``IDENT`` token (the method name)
#   children[2] = ``exprlist`` subtree (the args; optional for a
#                 no-arg call like ``x.size()``)
# Named constants keep the walker below ruff-clean (no PLR2004
# magic-number noise) and make the intent of the indexing
# obvious to a reader.
_MEMBER_DOT_ARG_RECEIVER = 0
_MEMBER_DOT_ARG_METHOD = 1
_MEMBER_DOT_ARG_ARGS = 2
_MEMBER_DOT_ARG_MIN_CHILDREN = 2  # must have receiver + method
_MEMBER_DOT_ARG_WITH_ARGS = 3  # receiver + method + args


def _is_macro_call(node: Any) -> bool:
    """Return True if *node* is a Lark Tree representing a CEL macro call.

    celpy parses ``receiver.method(args)`` as a ``member_dot_arg`` Tree
    with three children: the receiver ``member`` subtree, an ``IDENT``
    token naming the method, and an ``exprlist`` holding the args.
    A node is a macro call when the method name matches a known CEL
    macro — ``items.all(i, ...)`` is a macro, ``items.size()`` is not.
    """
    if not isinstance(node, Tree):
        return False
    if node.data != "member_dot_arg":
        return False
    if len(node.children) < _MEMBER_DOT_ARG_MIN_CHILDREN:
        return False
    method = node.children[_MEMBER_DOT_ARG_METHOD]
    if not isinstance(method, Token):
        return False
    return str(method) in _CEL_MACRO_METHODS


def _validate_expression_shape(
    tree: Tree,
    *,
    max_macro_nesting: int,
    max_macro_count: int,
) -> None:
    """Walk the Lark AST, enforcing macro-nesting and macro-count caps.

    Nesting is counted only when a macro sits inside another macro's
    *arg list* (predicate / projection), not when it sits in the
    receiver position. ``items.all(i, i > 0).filter(x, x < 10)`` is a
    chain (additive cost, nesting depth 1), while
    ``items.all(i, items.filter(j, ...))`` is truly nested
    (multiplicative cost, depth 2). Only the latter shape is what the
    cel-spec calls out as "the only avenue for exponential behavior",
    so only the latter shape counts toward the depth bound.

    Raises ``_CelExpressionShapeError`` on the first violation — by
    the time the first violation trips, we already know the expression
    is unsafe and further walking wastes cycles.
    """
    total_macros = 0

    def _walk(node: Any, nesting_depth: int) -> None:
        nonlocal total_macros
        if not isinstance(node, Tree):
            # Tokens and other leaves — nothing to recurse into.
            return

        if _is_macro_call(node):
            total_macros += 1
            if total_macros > max_macro_count:
                msg = (
                    f"CEL expression contains too many macro calls "
                    f"(max {max_macro_count})."
                )
                raise _CelExpressionShapeError(msg)
            new_nesting = nesting_depth + 1
            if new_nesting > max_macro_nesting:
                msg = (
                    f"CEL expression nests macros too deeply (max {max_macro_nesting})."
                )
                raise _CelExpressionShapeError(msg)

            # Receiver is chained, not nested — walk it at the SAME
            # depth so ``x.all(...).filter(...)`` doesn't falsely trip
            # the depth limit.
            if node.children:
                _walk(node.children[_MEMBER_DOT_ARG_RECEIVER], nesting_depth)

            # Arg list is lexically inside this macro's scope — walk
            # it at the incremented depth. This is where the
            # exponential shape lives: a macro inside a macro's
            # predicate multiplies the outer macro's iteration count.
            has_args = len(node.children) >= _MEMBER_DOT_ARG_WITH_ARGS
            if has_args and isinstance(node.children[_MEMBER_DOT_ARG_ARGS], Tree):
                for arg in node.children[_MEMBER_DOT_ARG_ARGS].children:
                    _walk(arg, new_nesting)
            return

        # Non-macro Tree node: walk every child at the current depth.
        for child in node.children:
            _walk(child, nesting_depth)

    _walk(tree, nesting_depth=0)


@lru_cache(maxsize=256)
def _compile_expr(expr: str) -> celpy.Program:
    """
    Compile a CEL expression into a reusable program.

    Runs the AST shape check (see :func:`_validate_expression_shape`)
    between Lark parse and celpy program construction, so a hostile
    expression is rejected before we build a program object for it.
    Cached by expression string — the check runs exactly once per
    unique expression that reaches this function. Exceptions are not
    cached by lru_cache, which is deliberate: repeated hostile
    submissions re-parse (cheap) but never poison the cache.
    """
    env = celpy.Environment()
    ast = env.compile(expr)
    _validate_expression_shape(
        ast,
        max_macro_nesting=CEL_MAX_MACRO_NESTING,
        max_macro_count=CEL_MAX_MACRO_COUNT,
    )
    return env.program(ast)


def _validate_context_shape(
    context: dict[str, Any],
    *,
    max_depth: int,
    max_total_symbols: int,
) -> None:
    """Walk the CEL context tree, enforcing depth and total-symbol bounds.

    Called before ``celpy.json_to_cel()`` so a pathologically nested or
    oversized payload is rejected before it burns CPU and memory on
    normalization. Counts dict keys and list items together as
    "symbols" — both represent a unit of work for the CEL runtime.

    Scalars are not counted (they're the leaves of the walk) but they
    also do not bump the depth — only dict/list descents do. This
    matches the intuition that ``{"a": 42}`` is shallower than
    ``{"a": {"b": 42}}``.

    Aliased containers are counted once. The CEL context shape set up
    by ``_build_cel_context`` intentionally aliases the payload dict
    under multiple namespace keys (``p``/``payload``, ``o``/``output``),
    so walking each top-level key naively would count the same
    underlying dict two or four times. Tracking visited objects by
    ``id()`` makes the bound reflect unique data-under-normalization,
    not namespace plumbing. This also defends against accidental
    self-references (shouldn't happen for JSON-shaped inputs but is
    free to guard).

    Raises ``_CelContextShapeError`` on the first violation. Tuples and
    sets are intentionally not handled: the public contract is a
    JSON-shaped ``dict[str, Any]`` (dicts/lists/scalars) and celpy will
    reject non-JSON containers anyway.
    """
    total_symbols = 0
    seen: set[int] = set()

    def _walk(obj: Any, depth: int) -> None:
        nonlocal total_symbols
        # Depth is counted against container nesting only — the check
        # lives inside each container branch rather than at function
        # entry so that a scalar at the deepest leaf doesn't spuriously
        # trip the limit. (``max_depth=3`` means "up to 3 levels of
        # dict/list nesting"; the scalars those containers hold are not
        # themselves a level.)
        if isinstance(obj, dict):
            obj_id = id(obj)
            if obj_id in seen:
                return
            seen.add(obj_id)
            if depth > max_depth:
                msg = f"CEL context nesting depth exceeds maximum ({max_depth})."
                raise _CelContextShapeError(msg)
            for value in obj.values():
                total_symbols += 1
                if total_symbols > max_total_symbols:
                    msg = (
                        f"CEL context total symbol count exceeds "
                        f"maximum ({max_total_symbols})."
                    )
                    raise _CelContextShapeError(msg)
                _walk(value, depth + 1)
        elif isinstance(obj, list):
            obj_id = id(obj)
            if obj_id in seen:
                return
            seen.add(obj_id)
            if depth > max_depth:
                msg = f"CEL context nesting depth exceeds maximum ({max_depth})."
                raise _CelContextShapeError(msg)
            for item in obj:
                total_symbols += 1
                if total_symbols > max_total_symbols:
                    msg = (
                        f"CEL context total symbol count exceeds "
                        f"maximum ({max_total_symbols})."
                    )
                    raise _CelContextShapeError(msg)
                _walk(item, depth + 1)
        # Scalars: stop recursing. They neither add depth nor count as
        # symbols on their own — only the containers that hold them do.

    _walk(context, depth=1)


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

    # Bound the cost of celpy.json_to_cel() normalization before it
    # runs — see refactor-step item ``[review-#4]``. The top-level
    # check above bounds the *name* surface the expression can see;
    # this check bounds the *work* normalization has to do on the
    # values behind those names.
    try:
        _validate_context_shape(
            context,
            max_depth=CEL_MAX_CONTEXT_DEPTH,
            max_total_symbols=CEL_MAX_CONTEXT_TOTAL_SYMBOLS,
        )
    except _CelContextShapeError as exc:
        return CelEvaluationResult(success=False, value=None, error=str(exc))

    # Compile (with AST shape check) on the request thread — a hostile
    # expression is rejected before we start a worker. The timeout below
    # exists to bound *evaluation*, not compilation; running the shape
    # check here means a macro-nested attack fails in microseconds
    # instead of burning a thread-pool slot. See refactor-step item
    # ``[review-§14.ast_check]``.
    try:
        program = _compile_expr(normalized)
    except _CelExpressionShapeError as exc:
        return CelEvaluationResult(success=False, value=None, error=str(exc))
    except Exception as exc:
        # Lark parse errors / invalid CEL syntax / other celpy failures.
        return CelEvaluationResult(success=False, value=None, error=str(exc))

    def _evaluate() -> Any:
        # Convert Python values → CEL native types (MapType, ListType, etc.)
        # so that dot-notation field selection works on maps, matching the
        # standard CEL spec behaviour used by Google's cel-go.  Without
        # this, plain Python dicts fail with "does not support field
        # selection" when accessed via dot notation (e.g., Materials.Material).
        cel_context = {
            k: celpy.json_to_cel(v) if isinstance(v, (dict, list)) else v
            for k, v in context.items()
        }
        return program.evaluate(cel_context)

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
