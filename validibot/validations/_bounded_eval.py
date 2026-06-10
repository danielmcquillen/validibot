"""Shared bounded executor for timing out untrusted expression evaluation.

CEL and JSONPath expressions authored into a workflow are shape-capped before
they ever run (see :mod:`validibot.validations.cel_eval` and
:mod:`validibot.validations.services._jsonpath_env`), but a slow-but-legal
expression can still take longer than we want a request thread to wait on. This
module provides a single, process-wide thread pool plus a wall-clock timeout
helper that both evaluators share.

WHY A SHARED, BOUNDED POOL — not a fresh ``ThreadPoolExecutor`` per call:

* It caps the number of expression-evaluation threads the process will ever
  spawn at :data:`_MAX_WORKERS`. A flood of slow or hostile expressions can no
  longer accumulate an unbounded pile of orphaned worker threads — that was the
  concern with creating a one-shot pool on every call.
* It removes per-call thread-creation overhead. CEL is evaluated *once per row*
  in tabular validation, so building and tearing down a pool per row was pure
  waste on the hot path.

WHY THE TIMEOUT IS NON-BLOCKING: a CPython thread cannot be force-killed, and a
CPU-bound evaluation holds the GIL, so on timeout we must NOT wait on the worker
— doing so would re-block the request thread and defeat the timeout entirely.
We stop waiting on the future and hand control back to the caller; the orphaned
worker keeps running on a pool thread until it finishes (bounded in practice by
the shape-caps the callers enforce up front), after which the thread is reused.
Truly *interrupting* such work needs a killable process boundary (process
isolation), which is a documented follow-up rather than something threads can
deliver.
"""

from __future__ import annotations

import concurrent.futures
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class ExpressionEvaluationTimeoutError(Exception):
    """Raised when a bounded expression evaluation exceeds its time budget.

    The worker is *not* cancelled (CPython cannot kill a running thread); it
    keeps draining in the background. Callers translate this into their own
    "timed out" result type.
    """


# Process-wide pool. Sized generously so legitimate concurrent validations are
# never starved of a worker, while still placing a hard ceiling on the absolute
# number of expression-evaluation threads the process can spawn. Each
# Gunicorn/worker process gets its own pool; threads are created lazily on first
# submit, so importing this module is cheap.
_MAX_WORKERS = max(8, (os.cpu_count() or 1) * 2)
_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_MAX_WORKERS,
    thread_name_prefix="vb-expr-eval",
)


def run_with_timeout[T](fn: Callable[[], T], *, timeout_s: float) -> T:
    """Run ``fn()`` on the shared pool, returning its result or timing out.

    Args:
        fn: A zero-argument callable performing the (already shape-capped)
            evaluation.
        timeout_s: Wall-clock budget in seconds.

    Returns:
        Whatever ``fn()`` returns, if it completes within ``timeout_s``.

    Raises:
        ExpressionEvaluationTimeout: If the budget is exceeded. The caller
            returns promptly; the worker is *not* waited on (it cannot be
            killed) and drains on a pool thread in the background.
        Exception: Any exception ``fn`` raises propagates unchanged.
    """
    future = _executor.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError as exc:
        # Deliberately do NOT wait on the GIL-holding worker — return control
        # to the caller immediately. The worker drains on its pool thread and
        # the thread is then reused.
        raise ExpressionEvaluationTimeoutError from exc
