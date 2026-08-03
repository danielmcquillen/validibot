"""Tests for the reusable GCP management Job command transport.

The operator sends one base64 payload as an execution override. These tests
prove that it becomes a normal argument vector without shell evaluation and
that malformed or failing commands stop the remote Job clearly.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from validibot.core.management.commands.run_encoded_management_command import (
    COMMAND_ENVIRONMENT_VARIABLE,
)


def test_encoded_command_runs_as_an_argument_vector(settings):
    """Quoted option values must reach Django intact without a shell."""
    command = 'example_command --reason "two words"'
    encoded = base64.b64encode(command.encode()).decode()
    completed = MagicMock(returncode=0)

    with (
        patch.dict(
            "os.environ",
            {COMMAND_ENVIRONMENT_VARIABLE: encoded},
            clear=False,
        ),
        patch(
            "validibot.core.management.commands.run_encoded_management_command."
            "subprocess.run",
            return_value=completed,
        ) as run,
    ):
        call_command("run_encoded_management_command")

    assert run.call_args.args[0] == [
        sys.executable,
        str(Path(settings.BASE_DIR) / "manage.py"),
        "example_command",
        "--reason",
        "two words",
    ]
    assert run.call_args.kwargs == {"check": False}


def test_encoded_command_rejects_invalid_base64():
    """A malformed execution override must fail before starting a child."""
    with (
        patch.dict(
            "os.environ",
            {COMMAND_ENVIRONMENT_VARIABLE: "not valid base64!"},
            clear=False,
        ),
        pytest.raises(CommandError, match="must encode a valid command"),
    ):
        call_command("run_encoded_management_command")


def test_encoded_command_propagates_child_failure():
    """The reusable Job must report the invoked command's non-zero status."""
    encoded = base64.b64encode(b"example_command").decode()
    with (
        patch.dict(
            "os.environ",
            {COMMAND_ENVIRONMENT_VARIABLE: encoded},
            clear=False,
        ),
        patch(
            "validibot.core.management.commands.run_encoded_management_command."
            "subprocess.run",
            return_value=MagicMock(returncode=7),
        ),
        pytest.raises(CommandError, match="status 7"),
    ):
        call_command("run_encoded_management_command")
