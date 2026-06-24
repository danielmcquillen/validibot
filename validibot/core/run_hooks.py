"""
Run-lifecycle hook registry for Validibot.

This mirrors ``core/policies.py`` (the org-policy *predicate* registry) but for
an **imperative, state-mutating** hook that runs INSIDE the run-launch
transaction. The two are complementary:

* ``check_org_policies`` answers a yes/no question *before* the run exists and
  outside any transaction — a cheap, side-effect-free gate.
* ``run_created_hooks`` runs *after* the ``ValidationRun`` row is created and
  still inside the launch ``transaction.atomic()`` — so a hook can atomically
  reserve resources against the just-created run under a row lock, and abort
  the launch by raising.

The community edition registers no hooks (so this is a no-op). Commercial
packages (validibot-cloud) register hooks at app-ready time. This follows the
same open-core pattern as ``core/license.py`` / ``core/features.py`` /
``core/policies.py``: core provides the hook, commercial packages provide the
logic.

**Raising is the sanctioned abort mechanism.** A hook that raises propagates the
exception out of the launch transaction, rolling back the run. That is how a
commercial reservation hook refuses a launch it cannot fund from *inside* the
transaction (the pre-transaction equivalent is returning ``(False, reason)``
from an org policy). Launch callers already handle the ``PermissionError`` that
``check_org_policies`` raises, so a hook raising ``PermissionError`` integrates
cleanly.

Usage in the launch path::

    from validibot.core.run_hooks import run_created_hooks

    with transaction.atomic():
        run = ValidationRun.objects.create(...)
        # Hooks may raise (e.g. PermissionError) to abort + roll back.
        run_created_hooks(run, workflow_type=workflow_type)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)

# A hook receives the just-created ``ValidationRun`` plus keyword context
# (e.g. ``workflow_type="ADVANCED"``) and returns nothing. It MAY raise to abort
# the launch — the exception propagates out of the launch transaction, rolling
# back the run. Hooks must accept ``**kwargs`` for forward compatibility.
RunCreatedHookFn = Callable[..., None]

# Internal registry — commercial packages register hooks here. Multiple hooks
# run in registration order; the first to raise aborts the launch.
_run_created_hooks: list[RunCreatedHookFn] = []


def register_run_created_hook(hook_fn: RunCreatedHookFn) -> None:
    """
    Register a hook fired inside the launch transaction, after run creation.

    Called by commercial packages at app-ready time. Multiple hooks run in
    registration order; the first to raise aborts the launch.

    Args:
        hook_fn: A callable taking ``(validation_run, **context)`` and
            returning ``None``. It may raise to abort the launch. Hook
            functions must accept ``**kwargs`` for forward compatibility.
    """
    _run_created_hooks.append(hook_fn)
    # Defensive name extraction: a hook may be a functools.partial or a callable
    # object without __module__/__qualname__, so fall back to repr rather than
    # letting an observability log line break registration.
    hook_name = getattr(hook_fn, "__qualname__", None) or repr(hook_fn)
    hook_module = getattr(hook_fn, "__module__", "?")
    logger.info("Run-created hook registered: %s.%s", hook_module, hook_name)


def run_created_hooks(validation_run: ValidationRun, **context) -> None:
    """
    Invoke all registered run-created hooks, in registration order.

    Runs inside the caller's ``transaction.atomic()``. A hook that raises
    propagates the exception (rolling back the launch) — this is intentional:
    it lets a commercial reservation hook refuse a launch it cannot fund.

    If no hooks are registered (community edition), this is a no-op.

    Args:
        validation_run: The just-created run (already persisted, same txn).
        **context: Keyword context passed through to each hook (e.g.
            ``workflow_type="ADVANCED"``, ``launching_user=<User>``). Hooks
            may use ``launching_user`` to waive enforcement for an operator
            (superuser), consistent with the bypass in ``check_org_policies``.
    """
    for hook_fn in _run_created_hooks:
        hook_fn(validation_run, **context)


def reset_run_created_hooks() -> None:
    """Clear all registered run-created hooks (for testing)."""
    _run_created_hooks.clear()
