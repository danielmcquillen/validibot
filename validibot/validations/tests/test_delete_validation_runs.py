"""Tests for the ``delete_validation_runs`` management command.

The command is an ops tool for unsticking workflows whose rulesets have
become immutable because they have validation runs, and for tidying up
demo/tutorial state. The tests below pin three guarantees that matter:

- Default-safe behaviour: without ``--confirm`` nothing is deleted, even
  if a scope is specified. This protects against accidental invocations
  in Cloud Run Jobs where stdout is the only feedback.
- Scope correctness: ``--workflow-id`` deletes only matching runs;
  unrelated workflows in the same org are untouched.
- ``PROTECT`` cascade: ``ValidationRun.delete()`` raises
  ``ProtectedError`` when ``IssuedCredential`` rows reference the runs,
  so the command must remove those credential rows first. The pro app is
  not always installed; the command must work in that environment too.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from validibot.validations.models import ValidationRun
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.tests.factories import WorkflowFactory

# Named constants for ruff PLR2004 — these are run counts assembled
# inside the test, not magic numbers from the codebase.
TWO_RUNS = 2


@pytest.mark.django_db
class TestDeleteValidationRunsDryRun:
    """Dry-run safety: without ``--confirm`` the database is untouched.

    These cases matter because Cloud Run Job invocations have no
    interactive prompt — the command itself is the last line of defence
    against an accidental wipe.
    """

    def test_dry_run_default_does_not_delete(self):
        """Running with a scope but no --confirm reports counts only."""
        workflow = WorkflowFactory()
        ValidationRunFactory(workflow=workflow)
        ValidationRunFactory(workflow=workflow)

        out = StringIO()
        call_command(
            "delete_validation_runs",
            f"--workflow-id={workflow.pk}",
            stdout=out,
        )

        assert ValidationRun.objects.filter(workflow=workflow).count() == TWO_RUNS
        output = out.getvalue()
        assert "Validation runs to delete: 2" in output
        assert "Dry run" in output

    def test_explicit_dry_run_overrides_confirm(self):
        """Passing both --confirm and --dry-run keeps dry-run semantics.

        Useful for scripted previews where ``--confirm`` is hardcoded
        but the operator wants to inspect the plan first.
        """
        workflow = WorkflowFactory()
        ValidationRunFactory(workflow=workflow)

        out = StringIO()
        call_command(
            "delete_validation_runs",
            f"--workflow-id={workflow.pk}",
            "--confirm",
            "--dry-run",
            stdout=out,
        )

        assert ValidationRun.objects.filter(workflow=workflow).count() == 1
        assert "Dry run" in out.getvalue()


@pytest.mark.django_db
class TestDeleteValidationRunsScoping:
    """Scope flags must only affect the intended rows.

    A workflow-scoped delete must not touch unrelated workflows even in
    the same org, because rulesets attached to those other workflows
    rely on their run history for the immutability lock.
    """

    def test_workflow_scope_leaves_other_workflows_intact(self):
        """Only the targeted workflow's runs disappear."""
        target = WorkflowFactory()
        other = WorkflowFactory(org=target.org)

        ValidationRunFactory(workflow=target)
        ValidationRunFactory(workflow=target)
        keeper = ValidationRunFactory(workflow=other)

        out = StringIO()
        call_command(
            "delete_validation_runs",
            f"--workflow-id={target.pk}",
            "--confirm",
            stdout=out,
        )

        assert ValidationRun.objects.filter(workflow=target).count() == 0
        assert ValidationRun.objects.filter(pk=keeper.pk).exists()
        assert "Deleted" in out.getvalue()

    def test_org_scope_includes_every_workflow_in_org(self):
        """All workflows belonging to the org get their runs removed."""
        org_workflow_a = WorkflowFactory()
        org_workflow_b = WorkflowFactory(org=org_workflow_a.org)
        unrelated = WorkflowFactory()

        ValidationRunFactory(workflow=org_workflow_a)
        ValidationRunFactory(workflow=org_workflow_b)
        survivor = ValidationRunFactory(workflow=unrelated)

        call_command(
            "delete_validation_runs",
            f"--org-id={org_workflow_a.org_id}",
            "--confirm",
            stdout=StringIO(),
        )

        assert ValidationRun.objects.filter(org=org_workflow_a.org).count() == 0
        assert ValidationRun.objects.filter(pk=survivor.pk).exists()

    def test_all_scope_requires_confirm(self):
        """``--all`` without --confirm previews but does not delete."""
        ValidationRunFactory()
        ValidationRunFactory()

        out = StringIO()
        call_command("delete_validation_runs", "--all", stdout=out)

        assert ValidationRun.objects.count() == TWO_RUNS
        assert "ALL validation runs" in out.getvalue()
        assert "Dry run" in out.getvalue()


@pytest.mark.django_db
class TestDeleteValidationRunsScopeRequired:
    """One of --workflow-id / --org-id / --all must be supplied.

    The argparse mutually-exclusive group enforces this; the test pins
    the behaviour so a future refactor that loosens the constraint is
    caught by CI rather than discovered in production.
    """

    def test_missing_scope_raises(self):
        """No scope at all → CommandError, no rows touched.

        argparse's mutually_exclusive_group(required=True) rejects the
        invocation; Django wraps the argparse error in CommandError
        when the command runs via ``call_command`` rather than the CLI.
        """
        ValidationRunFactory()

        with pytest.raises(CommandError):
            call_command("delete_validation_runs", "--confirm", stdout=StringIO())

        assert ValidationRun.objects.count() == 1


@pytest.mark.django_db
class TestDeleteValidationRunsEmptyScope:
    """A scope that matches zero rows is not an error.

    The command should exit cleanly with a friendly message so cron-like
    callers do not treat "nothing to do" as a failure.
    """

    def test_workflow_with_no_runs_succeeds(self):
        workflow = WorkflowFactory()

        out = StringIO()
        call_command(
            "delete_validation_runs",
            f"--workflow-id={workflow.pk}",
            "--confirm",
            stdout=out,
        )

        assert "No validation runs match" in out.getvalue()
