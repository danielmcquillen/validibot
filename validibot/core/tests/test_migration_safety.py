"""Tests for the pre-current-schema migration history guard.

The July 2026 migration reset is safe only for fresh databases or databases
already created from the new current-schema markers. These tests protect the
read-only preflight that prevents a legacy demo/staging database from reaching
duplicate schema operations during an otherwise routine deploy.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from validibot.core.migration_safety import incompatible_reset_migrations


def test_fresh_database_has_no_incompatible_history() -> None:
    """An empty recorder must remain legal for first installation."""
    assert (
        incompatible_reset_migrations(
            applied_migrations=set(),
            known_migrations={
                ("validations", "0026_current_schema"),
            },
        )
        == ()
    )


def test_deleted_migration_tail_is_reported() -> None:
    """A legacy post-cut migration must stop deployment before schema writes."""
    incompatible = incompatible_reset_migrations(
        applied_migrations={
            ("validations", "0025_fmu_allow_custom_assertion_targets"),
            ("validations", "0070_standardize_step_io_terminology"),
            ("workflows", "0033_standardize_step_output_reference_help"),
        },
        known_migrations={
            ("validations", "0025_fmu_allow_custom_assertion_targets"),
            ("validations", "0026_current_schema"),
            ("workflows", "0007_current_schema"),
        },
    )

    assert incompatible == (
        "validations.0070_standardize_step_io_terminology",
        "workflows.0033_standardize_step_output_reference_help",
    )


def test_completed_current_schema_cutover_tolerates_old_recorder_rows() -> None:
    """A recorded current marker proves that the app completed its cutover."""
    assert (
        incompatible_reset_migrations(
            applied_migrations={
                ("validations", "0026_current_schema"),
                ("validations", "0070_standardize_step_io_terminology"),
            },
            known_migrations={
                ("validations", "0026_current_schema"),
            },
        )
        == ()
    )


@patch("validibot.core.management.commands.check_migration_history.MigrationLoader")
@patch("validibot.core.management.commands.check_migration_history.MigrationRecorder")
def test_management_command_refuses_legacy_database(recorder_class, loader_class):
    """The operator-facing command must exit non-zero with the offending row."""
    recorder = MagicMock()
    recorder.applied_migrations.return_value = {
        ("validations", "0070_standardize_step_io_terminology")
    }
    recorder_class.return_value = recorder
    loader_class.return_value.disk_migrations = {
        ("validations", "0026_current_schema"): MagicMock()
    }

    with pytest.raises(CommandError, match=r"validations\.0070"):
        call_command("check_migration_history", stdout=StringIO())


@patch("validibot.core.management.commands.check_migration_history.MigrationLoader")
@patch("validibot.core.management.commands.check_migration_history.MigrationRecorder")
def test_management_command_accepts_current_database(recorder_class, loader_class):
    """A current database should pass with a concise deployment message."""
    recorder = MagicMock()
    recorder.applied_migrations.return_value = {("validations", "0026_current_schema")}
    recorder_class.return_value = recorder
    loader_class.return_value.disk_migrations = {
        ("validations", "0026_current_schema"): MagicMock()
    }
    stdout = StringIO()

    call_command("check_migration_history", stdout=stdout)

    assert "Migration history is compatible" in stdout.getvalue()
