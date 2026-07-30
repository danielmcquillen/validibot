"""Tests for the ``reset_system`` management command.

``reset_system`` is the deployment "factory reset": it permanently deletes all
operational data — validation runs, submissions, workflows, projects, managed
validator deployments, and validators — and then rebuilds the system validator
catalogue, while preserving users and organizations.

This suite exists because the command is irreversibly destructive and is meant
to be run against production environments. Three classes of regression would be
catastrophic, so each is pinned here:

  1. A confirmation gate that fails open (deletes without the exact phrase).
  2. A deletion that wipes something it should preserve (users/orgs) — or fails
     to wipe something it should.
  3. A ``ProtectedError`` from getting the FK ``PROTECT`` deletion order wrong,
     which would abort the whole reset partway through.

The fixtures deliberately build a dataset that exercises all relevant community
``PROTECT`` edges: ``ValidationRun.workflow``, ``WorkflowStep.validator``, and
``ValidatorExecutionDeployment.validator``. If the command deleted in the wrong
order, those edges would raise and the wipe tests would fail loudly.
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
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionProviderType
from validibot.validations.models import ValidationRun
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.validators.base.config import get_all_configs
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.tests.factories import WorkflowStepFactory


@pytest.fixture
def populated(db):
    """A minimal but PROTECT-complete dataset for reset tests.

    A single ``ValidationRunFactory`` transitively creates an organization, a
    user, a project, a workflow, and a submission. We then attach a
    ``WorkflowStep`` (which owns a ``Validator`` via ``PROTECT``) to that same
    workflow, then register a managed execution deployment which also protects
    the validator. The result touches every entity the reset deletes and every
    relevant ``PROTECT`` edge, so a single fixture validates the ordering
    contract.
    """
    run = ValidationRunFactory()
    step = WorkflowStepFactory(workflow=run.workflow)
    image_digest = f"sha256:{'a' * 64}"
    service_url = "https://validibot-reset-test.example.run.app"
    runtime_identity = "validator-runtime@reset-test.iam.gserviceaccount.com"
    ValidatorExecutionDeployment.objects.create(
        validator=step.validator,
        provider_type=ExecutionProviderType.GCP,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        display_name="Reset regression deployment",
        deployment_revision="reset-test-r1",
        provider_configuration={
            "project_id": "reset-test",
            "region": "australia-southeast1",
            "service_name": "validibot-reset-test",
            "service_url": service_url,
            "authentication_audience": service_url,
            "runtime_service_account": runtime_identity,
            "invoker_service_account": (
                "validator-invoker@reset-test.iam.gserviceaccount.com"
            ),
        },
        provider_resource_name=(
            "projects/reset-test/locations/australia-southeast1/"
            "services/validibot-reset-test"
        ),
        route=service_url,
        authentication_audience=service_url,
        backend_release_identity="0.1.0",
        backend_image_ref=(
            "ghcr.io/mcquilleninteractive/"
            f"validibot-validator-backend-energyplus@{image_digest}"
        ),
        backend_image_digest=image_digest,
        expected_runtime_identity=runtime_identity,
        declared_capabilities={
            "runtime_contract_version": "validibot-execution-v1",
            "maximum_execution_seconds": 300,
            "execution_shape": "REQUEST",
            "status_lookup": "UNSUPPORTED",
            "cancellation": "BEST_EFFORT",
            "storage_capability": "gcs_downscoped_token",
            "storage_isolation": "attempt_scoped",
            "architectures": ["linux-amd64"],
            "maximum_cpu_millis": 1000,
            "maximum_memory_mib": 1024,
            "callback_authentication": "ATTEMPT_NONCE_AND_OIDC",
        },
        maximum_execution_seconds=300,
        request_timeout_seconds=600,
        dispatch_timeout_seconds=1800,
        minimum_instances=0,
        maximum_instances=1,
        concurrency=1,
    )
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
        assert ValidatorExecutionDeployment.objects.count() == 1
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
        assert "- 1 validator deployments" in out.getvalue()
        assert Workflow.objects.count() == 1
        assert ValidatorExecutionDeployment.objects.count() == 1
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
        ``PROTECT`` ordering is correct: the fixture wires a run onto a workflow,
        a step onto a validator, and a managed deployment onto that validator.
        If the command deleted workflows before runs, or validators before
        steps and deployments, Django would raise ``ProtectedError`` and this
        test would fail instead of passing.
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
        assert ValidatorExecutionDeployment.objects.count() == 0

        # Validators are rebuilt from the current configs (not left empty). The
        # baseline "basic-validator" always ships, so its presence proves the
        # recreate path ran.
        assert Validator.objects.count() > 0
        assert Validator.objects.filter(slug="basic-validator").exists()

        # The rebuilt rows must exactly match the versions declared by the
        # current configs. This keeps reset_system aligned with sync_validators
        # when an individual validator intentionally advances its contract.
        expected_catalog = {(cfg.slug, cfg.version) for cfg in get_all_configs()}
        rebuilt_catalog = set(Validator.objects.values_list("slug", "version"))
        assert rebuilt_catalog == expected_catalog

        # Identity is untouched.
        assert Organization.objects.count() == orgs_before
        assert User.objects.count() == users_before

        assert "Deleted 1 validator deployment row(s)." in out.getvalue()
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
