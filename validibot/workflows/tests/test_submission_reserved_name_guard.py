"""Tests for the one-time ``submission`` reserved-name guard migration.

ADR-2026-06-03b reserves ``submission`` as the sixth CEL namespace. New signals
can no longer be named ``submission`` (enforced by ``validate_signal_name`` via
``RESERVED_CEL_NAMES``), but a row could have been created BEFORE the name was
reserved. Migration ``workflows/0028_guard_submission_reserved_name`` catches
such rows on deploy and refuses to migrate until they are renamed.

These tests exercise the migration's guard function directly against the test
database. We pass the live app registry as ``apps`` — the function only calls
``get_model``, and the field set is identical in the historical state — so the
test reflects exactly what runs during ``migrate``. The migration module is
loaded via ``importlib`` because its name begins with a digit.
"""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps as global_apps

from validibot.workflows.models import WorkflowSignalMapping
from validibot.workflows.tests.factories import WorkflowFactory

# The migration module name starts with a digit, so it cannot be imported with
# normal ``import`` syntax — load it dynamically.
_guard_migration = importlib.import_module(
    "validibot.workflows.migrations.0028_guard_submission_reserved_name",
)


@pytest.mark.django_db
def test_guard_is_noop_without_a_submission_named_signal():
    """The guard must pass cleanly when no row is named ``submission``.

    This is the common case on every deploy: ``submission`` was never an
    obvious signal name, so the guard should be a silent no-op and never block
    a migration for an innocent deployment.
    """
    WorkflowSignalMapping.objects.create(
        workflow=WorkflowFactory(),
        name="emissivity",
        source_path="materials[0].emissivity",
        on_missing="error",
        position=10,
    )
    # Must not raise.
    _guard_migration._guard_no_submission_named_signal(global_apps, None)


@pytest.mark.django_db
def test_guard_raises_for_a_signal_named_submission():
    """A pre-existing signal named ``submission`` must abort the migration.

    Renaming is required because the new ``submission.`` namespace would
    otherwise overlap a stale ``s.submission`` reference. The error must name
    the offending row and explain the remediation, so an operator can fix it
    without guesswork.

    Note we cannot ``create`` a ``submission``-named row directly — the model
    now rejects it (the reservation is enforced at save time). We therefore
    create a valid row and rewrite its name via ``.update()``, which bypasses
    model validation, faithfully simulating a row that predates the
    reservation — exactly the case the migration guard exists to catch.
    """
    mapping = WorkflowSignalMapping.objects.create(
        workflow=WorkflowFactory(),
        name="legacy_name",
        source_path="whatever",
        on_missing="error",
        position=10,
    )
    WorkflowSignalMapping.objects.filter(pk=mapping.pk).update(name="submission")

    with pytest.raises(RuntimeError, match="reserved CEL namespace"):
        _guard_migration._guard_no_submission_named_signal(global_apps, None)
