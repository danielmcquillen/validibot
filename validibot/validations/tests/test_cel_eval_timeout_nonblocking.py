"""Regression tests for the non-blocking CEL evaluation timeout.

WHY THIS SUITE EXISTS
---------------------
``evaluate_cel_expression`` bounds each CEL evaluation with a wall-clock timeout
so a slow evaluation cannot pin a request thread forever. A CPU-bound CEL
evaluation holds the GIL and cannot be interrupted from the outside, so the
worker keeps running after the timeout fires. The contract this suite guards is
that hitting the timeout returns control to the request thread *immediately*
rather than waiting on the un-killable worker — the regression being any change
that re-blocks on the worker (for example a ``shutdown(wait=True)`` on the
timeout path), which would make the request thread return only once the runaway
worker finished.

Evaluation runs on a shared, process-wide bounded thread pool
(``validibot.validations._bounded_eval``): on timeout the helper stops waiting
on the future and raises ``ExpressionEvaluationTimeoutError``, while the orphaned
worker drains on a pool thread in the background. These tests prove that
*liveness* contract — a hung evaluation surfaces as a timeout result promptly,
regardless of how long the worker keeps running. We simulate the
un-interruptible worker with a ``threading.Event`` the test controls, so the
assertion does not depend on real wall-clock CEL cost.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

from validibot.validations import cel_eval

# Generous slack between the (tiny) configured timeout and the assertion
# ceiling. If the blocking-shutdown regression returns, the call would only
# return after the worker is released (``RELEASE_AFTER_S``), which is far
# larger than this ceiling — so the test fails loudly rather than flakily.
TIMEOUT_MS = 50
PROMPT_RETURN_CEILING_S = 2.0
RELEASE_AFTER_S = 30.0


class _BlockingProgram:
    """Stand-in CEL program whose ``evaluate`` blocks until released.

    Models a CPU-bound CEL evaluation that the timeout cannot interrupt: the
    worker thread sits inside ``evaluate`` holding (conceptually) the GIL until
    the test explicitly releases it. This lets us assert the *request* thread
    returns promptly without waiting on the worker.
    """

    def __init__(self, release_event: threading.Event) -> None:
        """Store the event the worker waits on before returning."""
        self._release_event = release_event

    def evaluate(self, _context: dict[str, object]) -> object:
        """Block until released, emulating an un-interruptible evaluation."""
        self._release_event.wait(timeout=RELEASE_AFTER_S)
        return True


class TestCelTimeoutNonBlocking:
    """The timeout must free the request thread without waiting on the worker."""

    def test_timeout_returns_promptly_without_blocking_on_worker(self) -> None:
        """A hung evaluation must surface as a timeout result promptly.

        WHY IT MATTERS: this is the exact regression. With the old
        ``with``-block executor, ``__exit__``'s ``shutdown(wait=True)`` would
        block the request thread on the still-running worker, so the call would
        not return until ``RELEASE_AFTER_S`` elapsed. We assert the call
        returns a timeout result well inside ``PROMPT_RETURN_CEILING_S`` while
        the worker is still blocked — proving the request thread did not wait.
        """
        release_event = threading.Event()
        blocking_program = _BlockingProgram(release_event)

        try:
            # Patch the program builder so evaluation enters our blocking stub
            # instead of running real celpy. The expression/context still pass
            # the real shape-cap checks, so we exercise the genuine code path
            # right up to the executor timeout.
            with patch.object(
                cel_eval,
                "_build_program",
                return_value=blocking_program,
            ):
                start = time.monotonic()
                result = cel_eval.evaluate_cel_expression(
                    expression="value > 0",
                    context={"value": 1},
                    timeout_ms=TIMEOUT_MS,
                )
                elapsed = time.monotonic() - start

            assert result.success is False
            assert result.error == "CEL evaluation timed out."
            # The decisive assertion: we returned long before the worker was
            # released, so the request thread did not block on shutdown.
            assert elapsed < PROMPT_RETURN_CEILING_S
        finally:
            # Release the orphaned worker so it does not linger for the full
            # ``RELEASE_AFTER_S`` and slow the test session down.
            release_event.set()
