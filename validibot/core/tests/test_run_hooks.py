"""
Unit tests for the run-created hook registry (``validibot.core.run_hooks``).

This registry is the open-core extension point that lets commercial packages
(cloud) reserve resources INSIDE the run-launch transaction and abort a launch
by raising. The launcher relies on three behaviors, pinned here: registration,
in-order invocation, and exception propagation (the abort mechanism). The first
test also pins the community default — with no hooks registered, firing must be
a harmless no-op, which is what lets a community-only install launch runs at all.

These are pure-Python registry tests (no DB), so they use ``SimpleTestCase``.
The full launch-path integration (a cloud hook actually reserving credits and a
raise rolling the run back) is covered in the cloud metering reservation tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from django.test import SimpleTestCase

from validibot.core.run_hooks import register_run_created_hook
from validibot.core.run_hooks import reset_run_created_hooks
from validibot.core.run_hooks import run_created_hooks


class RunCreatedHookRegistryTests(SimpleTestCase):
    """Pin the registry contract the launcher depends on."""

    def setUp(self):
        # The registry is process-global; isolate each test from any hooks a
        # commercial app may have registered at startup.
        reset_run_created_hooks()

    def tearDown(self):
        reset_run_created_hooks()

    def test_no_hooks_registered_is_noop(self):
        """With no hooks, firing is a harmless no-op.

        This is the guarantee that a community-only install can launch runs: the
        launcher always calls ``run_created_hooks``, so with nothing registered
        it must do nothing rather than raise.
        """
        run = MagicMock()
        # Must not raise.
        run_created_hooks(run, workflow_type="ADVANCED")

    def test_registered_hook_receives_run_and_context(self):
        """A registered hook receives the run plus the keyword context.

        The launcher passes ``workflow_type`` so a cloud hook can decide whether
        to reserve (ADVANCED) or skip (BASIC); this pins that pass-through so a
        future signature change can't silently drop it.
        """
        hook = MagicMock()
        register_run_created_hook(hook)

        run = MagicMock()
        run_created_hooks(run, workflow_type="ADVANCED")

        hook.assert_called_once_with(run, workflow_type="ADVANCED")

    def test_hooks_run_in_registration_order(self):
        """Multiple hooks fire in registration order.

        Order is part of the contract: a later hook may rely on an earlier one's
        effect. Pinning it prevents a silent reordering regression.
        """
        calls: list[str] = []
        register_run_created_hook(lambda run, **kw: calls.append("first"))
        register_run_created_hook(lambda run, **kw: calls.append("second"))

        run_created_hooks(MagicMock())

        self.assertEqual(calls, ["first", "second"])

    def test_raising_hook_propagates_to_abort_launch(self):
        """A hook that raises propagates — this IS the abort mechanism.

        A cloud reservation hook refuses a launch it cannot fund by raising from
        inside the launch transaction; the exception must NOT be swallowed, so
        the surrounding ``transaction.atomic()`` rolls the just-created run back.
        Swallowing it here would let an unfunded run proceed.
        """

        def refuse(run, **kw):
            raise PermissionError("insufficient credits")

        register_run_created_hook(refuse)

        with pytest.raises(PermissionError):
            run_created_hooks(MagicMock())
