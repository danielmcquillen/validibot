"""Tests for the ``prune_duplicate_system_validators`` management command.

Background
----------
The ``Validator`` table is keyed by ``UniqueConstraint(slug, version)``, not
by ``slug`` alone. When a validator config's ``version`` is bumped (e.g.
the SHACL validator went from an earlier version to ``"0.2"``),
``sync_validators`` creates a new row at the new ``(slug, version)`` pair and
leaves the previous row in place. Self-hosted databases upgraded across one
of those bumps end up with two cards in the "Add workflow step" picker (one
per row) — same slug, same name, different version, identical to a user's
eyes.

Why this matters
----------------
The leftover row is more than a cosmetic issue:

  - ``Validator.workflowstep_set`` uses ``on_delete=PROTECT``, so a workflow
    that originally locked onto the stale row keeps it pinned even after
    ``sync_validators`` runs — naive deletes raise ``ProtectedError``.
  - Stale rows accumulate orphan ``SignalDefinition`` / ``Derivation`` rows
    on ``CASCADE``, so dropping them indirectly through the canonical row
    would lose data the operator might not realise was attached.

These tests prove the prune command:

  1. Identifies the canonical row via the currently-declared config version
  2. Reassigns ``WorkflowStep.validator`` and ``ValidatorResourceFile.validator``
     FKs from stale rows to the canonical row before delete
  3. Drops the stale rows (and their CASCADE-attached signals/derivations)
  4. Respects ``--commit`` vs. dry-run so an operator can preview safely
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import ValidationType
from validibot.validations.models import SignalDefinition
from validibot.validations.models import Validator
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import SignalDefinitionFactory
from validibot.validations.tests.factories import StepSignalBindingFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.factories import ValidatorResourceFileFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


class PruneDuplicateSystemValidatorsTests(TestCase):
    """End-to-end behaviour of the duplicate-system-validator prune command."""

    def _call(self, *args, **kwargs):
        out, err = StringIO(), StringIO()
        call_command(
            "prune_duplicate_system_validators",
            *args,
            stdout=out,
            stderr=err,
            **kwargs,
        )
        return out.getvalue(), err.getvalue()

    # ── Detection: dry-run reporting ────────────────────────────────────
    # The command's primary safety guarantee is "show before you delete".
    # A wrong canonical pick is a data-loss event because WorkflowStep FKs
    # get reassigned, so dry-run output is the operator's chance to verify.

    def test_dry_run_does_not_modify_database(self):
        """Without ``--commit`` the command must be a pure observer.

        Operators run dry-run first to check that the pick of canonical row
        looks sane. If dry-run secretly mutates state, that contract breaks
        and the safety net is gone.
        """
        # Two SHACL rows: stale empty-version + current "0.2".
        stale = ValidatorFactory(
            slug="shacl-validator",
            version="",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )
        canonical = ValidatorFactory(
            slug="shacl-validator",
            version="0.2",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )

        out, _ = self._call()

        # Both rows still present.
        self.assertTrue(Validator.objects.filter(pk=stale.pk).exists())
        self.assertTrue(Validator.objects.filter(pk=canonical.pk).exists())
        # Dry-run banner is shown so the operator knows nothing was applied.
        self.assertIn("DRY-RUN", out)
        self.assertIn("shacl-validator", out)

    def test_no_duplicates_reports_clean(self):
        """When the DB is healthy the command must say so loud and clear.

        Operators will run this on production-like databases to check for
        damage from earlier upgrades. A silent "no output" run would be
        ambiguous — was nothing found, or did the command skip itself?
        """
        ValidatorFactory(slug="basic-validator", version="1.0", is_system=True)

        out, _ = self._call()

        self.assertIn("No duplicate system validators found", out)

    # ── Canonical pick: config version wins ─────────────────────────────
    # The canonical row should match what `sync_validators` would have
    # created today. The currently-declared config version is the source
    # of truth for "this is the one we keep."

    def test_canonical_pick_matches_declared_config_version(self):
        """The row keeping the FK references must match the config version.

        If the prune picks the wrong row, every workflow that referenced the
        stale row gets moved to a *different* stale row — replacing one mess
        with another. Pinning to the declared config version mirrors what
        sync_validators would do today and keeps the picker consistent.
        """
        # SHACL config declares version="0.2"; the older row at "" is stale.
        stale = ValidatorFactory(
            slug="shacl-validator",
            version="",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )
        canonical = ValidatorFactory(
            slug="shacl-validator",
            version="0.2",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )

        self._call("--commit")

        self.assertFalse(Validator.objects.filter(pk=stale.pk).exists())
        self.assertTrue(Validator.objects.filter(pk=canonical.pk).exists())

    # ── FK reassignment: WorkflowStep (PROTECT) ─────────────────────────
    # WorkflowStep.validator uses on_delete=PROTECT, so any attempt to
    # delete a validator referenced by a step raises ProtectedError. The
    # prune command MUST move steps to the canonical row first.

    def test_workflow_steps_are_reassigned_before_delete(self):
        """Workflow steps referencing the stale validator move to canonical.

        WorkflowStep.validator is PROTECT'd. If the command deleted the stale
        row without reassigning, Postgres would raise ProtectedError and the
        whole atomic block would roll back. Reassignment is the only path
        forward; this test pins it.
        """
        org = OrganizationFactory()
        stale = ValidatorFactory(
            slug="shacl-validator",
            version="",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )
        canonical = ValidatorFactory(
            slug="shacl-validator",
            version="0.2",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )
        workflow = WorkflowFactory(org=org)
        step = WorkflowStepFactory(workflow=workflow, validator=stale)

        self._call("--commit")

        step.refresh_from_db()
        self.assertEqual(step.validator_id, canonical.pk)
        self.assertFalse(Validator.objects.filter(pk=stale.pk).exists())

    # ── FK reassignment: ValidatorResourceFile (CASCADE) ────────────────
    # Resource files CASCADE on delete, so leaving them on the stale row
    # would silently destroy them. Reassigning is the only data-preserving
    # path.

    def test_resource_files_are_reassigned_before_delete(self):
        """Resource files attached to a stale validator survive the merge.

        CASCADE would silently drop them if the stale row were deleted
        without reassignment. For library validators with uploaded SHACL
        shape files this would be silent data loss — exactly the kind of
        failure mode operators should never have to recover from.
        """
        stale = ValidatorFactory(
            slug="shacl-validator",
            version="",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )
        canonical = ValidatorFactory(
            slug="shacl-validator",
            version="0.2",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )
        resource = ValidatorResourceFileFactory(validator=stale)

        self._call("--commit")

        resource.refresh_from_db()
        self.assertEqual(resource.validator_id, canonical.pk)

    def test_signal_references_are_remapped_before_stale_delete(self):
        """Assertions and bindings must not be orphaned by stale signal delete.

        Stale system validators own stale ``SignalDefinition`` rows. Deleting
        the stale validator cascades those signals, so references must move to
        the matching canonical signal first or workflow assertions/bindings
        silently lose their target.
        """
        org = OrganizationFactory()
        stale = ValidatorFactory(
            slug="shacl-validator",
            version="",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )
        canonical = ValidatorFactory(
            slug="shacl-validator",
            version="0.2",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )
        stale_signal = SignalDefinitionFactory(
            validator=stale,
            contract_key="shacl_total_count",
            direction=SignalDirection.OUTPUT,
        )
        canonical_signal = SignalDefinitionFactory(
            validator=canonical,
            contract_key="shacl_total_count",
            direction=SignalDirection.OUTPUT,
        )
        workflow = WorkflowFactory(org=org)
        step = WorkflowStepFactory(workflow=workflow, validator=stale)
        ruleset = RulesetFactory(org=org)
        assertion = RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.BASIC,
            operator=AssertionOperator.LE,
            target_signal_definition=stale_signal,
            target_data_path="",
        )
        binding = StepSignalBindingFactory(
            workflow_step=step,
            signal_definition=stale_signal,
        )

        self._call("--commit")

        assertion.refresh_from_db()
        binding.refresh_from_db()
        self.assertEqual(assertion.target_signal_definition_id, canonical_signal.pk)
        self.assertEqual(binding.signal_definition_id, canonical_signal.pk)
        self.assertFalse(SignalDefinition.objects.filter(pk=stale_signal.pk).exists())

    # ── Safety: org validators are off-limits ───────────────────────────
    # Two org-owned SHACL library validators with the same slug are a
    # legitimate situation (different orgs uploading their own shapes).
    # The prune command must only touch is_system=True rows.

    def test_custom_org_validators_are_never_touched(self):
        """Org-owned (is_system=False) rows are out of scope, full stop.

        Library SHACL validators live in the same Validator table with
        ``is_system=False``. They legitimately share slugs across versions
        (an org bumping its own library validator). Touching them from a
        system-cleanup command would risk cross-tenant data damage — the
        worst-case bug for a multi-tenant app. This test pins the boundary
        by setting up an org-owned slug collision that the command must
        leave alone, while a *system* duplicate of a different slug is
        present in the same DB so we know the command did run something.
        """
        org_a = OrganizationFactory()
        org_b = OrganizationFactory()
        # Same slug, distinct versions, both org-owned: allowed by the
        # (slug, version) unique constraint and explicitly out of scope
        # for this command.
        a = ValidatorFactory(
            slug="my-shacl",
            version="1",
            is_system=False,
            org=org_a,
        )
        b = ValidatorFactory(
            slug="my-shacl",
            version="2",
            is_system=False,
            org=org_b,
        )
        # A real system duplicate so the command isn't a no-op overall.
        ValidatorFactory(
            slug="shacl-validator",
            version="",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )
        ValidatorFactory(
            slug="shacl-validator",
            version="0.2",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )

        self._call("--commit")

        self.assertTrue(Validator.objects.filter(pk=a.pk).exists())
        self.assertTrue(Validator.objects.filter(pk=b.pk).exists())

    # ── Idempotency ─────────────────────────────────────────────────────
    # Running the command twice in a row should be a no-op on the second
    # run. That's the contract for any cleanup tool — operators should be
    # able to re-run safely after partial failures or schedule it
    # defensively.

    def test_idempotent_when_run_twice(self):
        """A second run after a clean commit must be a no-op.

        Operators may schedule this command as part of a deploy hook or
        re-run it after a partial failure. Either way, "run twice" must
        match "run once" — anything else turns the cleanup into its own
        source of bugs.
        """
        ValidatorFactory(
            slug="shacl-validator",
            version="",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )
        ValidatorFactory(
            slug="shacl-validator",
            version="0.2",
            validation_type=ValidationType.SHACL,
            is_system=True,
        )

        self._call("--commit")
        # Second run sees only the canonical row.
        out, _ = self._call("--commit")

        self.assertIn("No duplicate system validators found", out)
