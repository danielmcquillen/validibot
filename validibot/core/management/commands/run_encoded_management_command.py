"""Run one encoded Django command without routing it through a shell.

The reusable GCP management Job receives each operator command as a base64
environment override.  Decoding into an argument vector keeps one stable Job
definition while preserving Django's normal command-line parsing and avoiding
shell interpolation of operator-supplied values.
"""

from __future__ import annotations

import base64
import binascii
import os
import shlex
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

COMMAND_ENVIRONMENT_VARIABLE = "VALIDIBOT_MANAGEMENT_COMMAND_B64"


class Command(BaseCommand):
    """Execute exactly one base64-encoded ``manage.py`` argument vector."""

    help = "Run the management command supplied by the reusable GCP Job."

    def handle(self, *args, **options):
        """Decode, validate, and execute the requested command without a shell."""
        encoded = os.environ.get(COMMAND_ENVIRONMENT_VARIABLE, "")
        if not encoded:
            raise CommandError(f"{COMMAND_ENVIRONMENT_VARIABLE} is required")
        try:
            command_text = base64.b64decode(encoded, validate=True).decode("utf-8")
            command_arguments = shlex.split(command_text)
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise CommandError(
                f"{COMMAND_ENVIRONMENT_VARIABLE} must encode a valid command"
            ) from exc
        if not command_arguments:
            raise CommandError("The encoded management command is empty")

        manage_py = Path(settings.BASE_DIR) / "manage.py"
        completed = subprocess.run(  # noqa: S603 - validated argument vector, no shell
            [sys.executable, str(manage_py), *command_arguments],
            check=False,
        )
        if completed.returncode:
            raise CommandError(
                f"Management command exited with status {completed.returncode}"
            )
