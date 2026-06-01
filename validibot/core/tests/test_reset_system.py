"""Tests for the ``reset_system`` management command.

``reset_system`` is the deployment "factory reset": it permanently deletes all
operational data — validation runs, submissions, workflows, projects, and
validators — and then rebuilds the system validator catalogue, while preserving
users and organizations.

This suite exists because the command is irreversibly destructive and is meant
to be run against production environments. Three classes of regression would be
catastrophic, so each is pinned here:

  1. A confirmation gate that fails open (deletes without the exact phrase).
  2. A deletion that wipes something it should preserve (users/orgs) — or fails
     to wipe something it should.
  3. A ``ProtectedError`` from getting the FK ``PROTECT`` deletion order wrong,
     which would abort the whole reset partway through.

The fixtures deliberately build a dataset that exercises both community
``PROTECT`` edges: ``ValidationRun.workflow`` and ``WorkflowStep.validator``. If
the command deleted in the wrong order, those edges would raise and the wipe
tests would fail loudly.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from validibot.core.management.commands.reset_system import CONFIRM_PHRASE
from validibot.projects.models import Project
from validibot.submissions.models import Submission
from validibot.users.models import Organization
from validibot.users.models import User
from validibot.validations.models import ValidationRun
from validibot.validations.models import Validator
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.tests.factories import WorkflowStepFactory


@pytest.fixture
def populated(db):
    """A minimal but PROTECT-complete dataset for reset tests.

    A single ``ValidationRunFactory`` transitively creates an organization, a
    user, a project, a workflow, and a submission. We then attach a
    ``WorkflowStep`` (which owns a ``Validator`` via ``PROTECT``) to that same
    workflow. The result touches every entity the reset deletes *and* both
    ``PROTECT`` edges the deletion order has to respect, so a single fixture
    validates the ordering contract.
    """
    run = ValidationRunFactory()
    WorkflowStepFactory(workflow=run.workflow)
    return run


# ─────────────────────────────────────────────────────────────────────────────
# Confirmation gate
#
# The single most important property of a destructive command is that it does
# NOTHING unless explicitly authorised. These tests assert the gate fails
# *closed*: the default, a wrong phrase, and a forced dry-run all leave data
# untouched.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestConfirmationGate:
    """The reset must never delete data without the exact confirmation phrase."""

    def test_default_is_dry_run_and_deletes_nothing(self, populated):
        """Running with no ``--confirm`` must preview only, never delete.

        This is the accidental-invocation case (a CI job, a fat-fingered
        command). If the default ever became "delete", a single stray
        invocation would wipe production — so the default behaviour is the
        most important thing to lock down.
        """
        out = StringIO()
        call_command("reset_system", stdout=out)

        assert "DRY RUN" in out.getvalue()
        # Every entity is still present.
        assert ValidationRun.objects.count() == 1
        assert Submission.objects.count() == 1
        assert Workflow.objects.count() == 1
        assert WorkflowStep.objects.count() == 1
        assert Validator.objects.count() == 1

    def test_wrong_phrase_errors_and_deletes_nothing(self, populated):
        """A wrong ``--confirm`` value must raise, not silently dry-run.

        A typo in the confirmation phrase must be treated as a hard error so it
        can never be mistaken for a successful (but empty) run. Surfacing it as
        a non-zero ``CommandError`` makes the mistake obvious to the operator
        and to any wrapping automation.
        """
        with pytest.raises(CommandError):
            call_command("reset_system", confirm="not-the-phrase")

        assert ValidationRun.objects.count() == 1
        assert Workflow.objects.count() == 1

    def test_force_dry_run_overrides_correct_phrase(self, populated):
        """``--dry-run`` must win even when the correct phrase is supplied.

        Operators use ``--dry-run`` to preview a reset they fully intend to run,
        without committing. If the correct phrase silently overrode the
        dry-run flag, that preview would become a live wipe — the opposite of
        what was asked.
        """
        out = StringIO()
        call_command(
            "reset_system",
            confirm=CONFIRM_PHRASE,
            dry_run=True,
            stdout=out,
        )

        assert "DRY RUN" in out.getvalue()
        assert Workflow.objects.count() == 1
        assert Validator.objects.count() == 1


# ─────────────────────────────────────────────────────────────────────────────
# Full reset behaviour
#
# With the correct phrase, the command must wipe exactly the in-scope entities,
# preserve users and organizations, and rebuild the validator catalogue — all
# without tripping a PROTECT constraint.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestFullReset:
    """The authorised reset wipes scope, preserves identity, rebuilds validators."""

    def test_reset_wipes_scope_preserves_identity_and_rebuilds(self, populated):
        """The happy path: confirm, wipe, preserve users/orgs, rebuild validators.

        This is the end-to-end contract. It also implicitly proves the
        ``PROTECT`` ordering is correct: the fixture wires a run onto a workflow
        (``PROTECT``) and a step onto a validator (``PROTECT``). If the command
        deleted workflows before runs, or validators before steps, Django would
        raise ``ProtectedError`` and this test would fail instead of passing.
        """
        # Identity rows we expect to survive the wipe.
        orgs_before = Organization.objects.count()
        users_before = User.objects.count()
        assert orgs_before >= 1
        assert users_before >= 1

        out = StringIO()
        # interactive=False mirrors the non-interactive Cloud Run path and keeps
        # the test independent of whether pytest's stdin happens to be a TTY.
        call_command(
            "reset_system",
            confirm=CONFIRM_PHRASE,
            interactive=False,
            stdout=out,
        )

        # In-scope entities are gone.
        assert ValidationRun.objects.count() == 0
        assert Submission.objects.count() == 0
        assert Workflow.objects.count() == 0
        assert WorkflowStep.objects.count() == 0
        assert Project.objects.count() == 0

        # Validators are rebuilt from the current configs (not left empty). The
        # baseline "basic-validator" always ships, so its presence proves the
        # recreate path ran.
        assert Validator.objects.count() > 0
        assert Validator.objects.filter(slug="basic-validator").exists()

        # Every rebuilt validator lands at the clean v1 baseline. This pins the
        # "reset versions to 1" requirement: the configs themselves now declare
        # version 1, so a rebuild can never produce a v2/v3 row. The EnergyPlus
        # validator (historically v3) is the canonical proof it collapsed to v1.
        assert Validator.objects.exclude(version=1).count() == 0
        assert Validator.objects.filter(
            slug="energyplus-idf-validator",
            version=1,
        ).exists()

        # Identity is untouched.
        assert Organization.objects.count() == orgs_before
        assert User.objects.count() == users_before

        assert "System reset complete" in out.getvalue()

    def test_reset_is_safe_on_an_empty_database(self, db):
        """Resetting a fresh instance must succeed and still build validators.

        The command should be safe to run on an instance that has no runs,
        workflows, or projects yet — deleting zero rows is not an error, and the
        validator rebuild must still populate the catalogue. This guards the
        first-run / re-run idempotency the command's docstring promises.
        """
        out = StringIO()
        call_command(
            "reset_system",
            confirm=CONFIRM_PHRASE,
            interactive=False,
            stdout=out,
        )

        assert "System reset complete" in out.getvalue()
        assert Validator.objects.filter(slug="basic-validator").exists()
