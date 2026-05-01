"""Tests for the ruleset & ruleset-assertion immutability gates.

ADR-2026-04-27 Phase 3 Session C, task 10: once a Ruleset is
referenced by any step on a locked or used workflow, mutations to
the rules themselves (and to the assertions belonging to that
ruleset) are silently rewriting what previously-launched runs were
checking against. The model's ``clean()`` raises
``ValidationError`` to make this loud.

What this file pins down
========================

1. ``Ruleset.is_used_by_locked_workflow()`` — the canonical
   "is anyone consuming me?" check. Detects both the direct
   ``WorkflowStep.ruleset`` linkage and the locked/used workflow
   side of the relationship.
2. ``Ruleset.clean()`` blocks mutation of the ruleset's rule
   definition (``rules_text``, ``rules_file``, ``metadata``,
   ``ruleset_type``) when the ruleset is in use.
3. ``Ruleset.clean()`` ALLOWS mutation of cosmetic / identity
   fields (``name``, ``version``, ``user``) — renaming a ruleset
   doesn't change what it asserts.
4. ``RulesetAssertion.clean()`` blocks mutation of an existing
   assertion's semantic fields (operator, target, rhs, options,
   when_expression, severity).
5. ``RulesetAssertion.clean()`` blocks ADDING a new assertion to
   a ruleset that's already in use.
6. The gate is dormant for unused rulesets — fresh authoring
   path stays unblocked.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.test import TestCase

from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import RulesetType
from validibot.validations.models import RulesetAssertion
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


# ──────────────────────────────────────────────────────────────────────
# Ruleset.is_used_by_locked_workflow — the lookup
# ──────────────────────────────────────────────────────────────────────


class RulesetIsUsedByLockedWorkflowTests(TestCase):
    """The canonical "is this ruleset in use?" detector.

    This is the foundation for every gate below — get the lookup
    wrong and the immutability check either over- or under-fires.
    """

    def test_returns_false_for_orphan_ruleset(self):
        """A ruleset with no workflow step references is not "in use"."""
        ruleset = RulesetFactory()
        assert ruleset.is_used_by_locked_workflow() is False

    def test_returns_false_for_unused_unlocked_workflow(self):
        """Step references an unlocked workflow with no runs -> not in use."""
        ruleset = RulesetFactory()
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow, ruleset=ruleset)
        assert ruleset.is_used_by_locked_workflow() is False

    def test_returns_true_for_step_on_locked_workflow(self):
        """Locked workflow with a step using this ruleset -> in use."""
        ruleset = RulesetFactory()
        workflow = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=workflow, ruleset=ruleset)
        assert ruleset.is_used_by_locked_workflow() is True

    def test_returns_true_when_workflow_has_runs(self):
        """Workflow with a run — even if not locked — counts as in use.

        The ADR's gate fires on either ``is_locked`` or
        ``has_runs``: a workflow that's been launched has past runs
        whose contract we shouldn't silently re-write.
        """
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.tests.factories import ValidationRunFactory

        ruleset = RulesetFactory()
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow, ruleset=ruleset)
        submission = SubmissionFactory(workflow=workflow)
        ValidationRunFactory(workflow=workflow, submission=submission)

        assert ruleset.is_used_by_locked_workflow() is True


# ──────────────────────────────────────────────────────────────────────
# Ruleset.clean — semantic-field gate
# ──────────────────────────────────────────────────────────────────────


class RulesetCleanImmutabilityTests(TestCase):
    """``clean()`` raises when in-use rulesets mutate semantic fields."""

    def _make_used_ruleset(self):
        """Build a ruleset attached to a locked workflow's step."""
        ruleset = RulesetFactory(
            ruleset_type=RulesetType.JSON_SCHEMA,
            metadata={"schema_type": JSONSchemaVersion.DRAFT_2020_12.value},
            rules_text='{"type": "object"}',
        )
        workflow = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=workflow, ruleset=ruleset)
        return ruleset

    def test_clean_blocks_rules_text_change(self):
        """Editing ``rules_text`` on a used ruleset -> ValidationError."""
        ruleset = self._make_used_ruleset()
        ruleset.rules_text = '{"type": "string"}'  # different schema
        with pytest.raises(ValidationError) as exc:
            ruleset.clean()
        assert "rules_text" in exc.value.message_dict

    def test_clean_blocks_metadata_change(self):
        """Changing ``metadata`` (e.g. schema_type) on a used ruleset blocks."""
        ruleset = self._make_used_ruleset()
        # Switch to a different JSON Schema dialect — semantic change.
        ruleset.metadata = {"schema_type": JSONSchemaVersion.DRAFT_07.value}
        with pytest.raises(ValidationError) as exc:
            ruleset.clean()
        assert "metadata" in exc.value.message_dict

    def test_clean_blocks_ruleset_type_change(self):
        """Switching ``ruleset_type`` is a fundamental rule-engine swap."""
        ruleset = self._make_used_ruleset()
        ruleset.ruleset_type = RulesetType.XML_SCHEMA
        # Don't bother with metadata — the gate fires before any
        # other check would care.
        with pytest.raises(ValidationError):
            ruleset.clean()

    def test_clean_allows_name_rename(self):
        """``name`` is identity / cosmetic — renaming doesn't change rules."""
        ruleset = self._make_used_ruleset()
        ruleset.name = "Renamed but rules unchanged"
        # Should NOT raise.
        ruleset.clean()

    def test_clean_allows_version_change(self):
        """``version`` is a label — bumping it doesn't re-write past runs."""
        ruleset = self._make_used_ruleset()
        ruleset.version = "2.0"
        ruleset.clean()

    def test_clean_allows_no_op_save(self):
        """Hitting save twice with no real changes must not falsely fail.

        Otherwise reloading the admin edit page on a locked ruleset
        would be effectively unusable — the page would always error
        because clean() runs even when nothing changed.
        """
        ruleset = self._make_used_ruleset()
        # No mutations — clean against the same DB row.
        ruleset.clean()


class RulesetCleanFreshAuthoringTests(TestCase):
    """The gate is dormant for unused rulesets.

    A user authoring a brand-new ruleset, or iterating on one that
    no workflow has launched yet, should never see the gate fire.
    Without these tests we'd risk a regression that locks ALL
    rulesets — over-application of the trust property.
    """

    def test_clean_allows_rules_text_change_on_unused_ruleset(self):
        """Fresh ruleset: any field can change."""
        ruleset = RulesetFactory(
            rules_text='{"type": "object"}',
        )
        ruleset.rules_text = '{"type": "array"}'
        ruleset.clean()  # must not raise

    def test_clean_allows_changes_when_only_unlocked_workflow_uses_it(self):
        """Step on an unlocked workflow doesn't trigger the gate."""
        ruleset = RulesetFactory(rules_text='{"type": "object"}')
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow, ruleset=ruleset)

        ruleset.rules_text = '{"type": "string"}'
        ruleset.clean()


# ──────────────────────────────────────────────────────────────────────
# RulesetAssertion.clean — assertion-level gate
# ──────────────────────────────────────────────────────────────────────


class RulesetAssertionImmutabilityTests(TestCase):
    """Adding, removing, or editing an assertion changes the ruleset's behavior.

    The gate mirrors the parent ruleset's: when the parent is in use,
    individual assertions cannot mutate semantic fields, and new
    assertions cannot be attached.
    """

    def _make_used_ruleset_with_assertion(self):
        """Build (ruleset, assertion) where the ruleset is locked-in-use."""
        ruleset = RulesetFactory()
        assertion = RulesetAssertionFactory(
            ruleset=ruleset,
            operator=AssertionOperator.LE,
            target_data_path="$.value",
            rhs={"value": 100},
        )
        workflow = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=workflow, ruleset=ruleset)
        return ruleset, assertion

    def test_clean_blocks_operator_change(self):
        """Switching the operator (LE -> GE) inverts the rule."""
        _, assertion = self._make_used_ruleset_with_assertion()
        assertion.operator = AssertionOperator.GE
        with pytest.raises(ValidationError) as exc:
            assertion.clean()
        assert "operator" in exc.value.message_dict

    def test_clean_blocks_rhs_change(self):
        """Changing the operand value (100 -> 200) is a behavior change."""
        _, assertion = self._make_used_ruleset_with_assertion()
        assertion.rhs = {"value": 200}
        with pytest.raises(ValidationError) as exc:
            assertion.clean()
        assert "rhs" in exc.value.message_dict

    def test_clean_blocks_target_change(self):
        """Repointing target_data_path checks a different field."""
        _, assertion = self._make_used_ruleset_with_assertion()
        assertion.target_data_path = "$.other"
        with pytest.raises(ValidationError) as exc:
            assertion.clean()
        assert "target_data_path" in exc.value.message_dict

    def test_clean_blocks_severity_change(self):
        """Severity controls if a failure is ERROR vs WARN — affects pass/fail."""
        from validibot.validations.constants import Severity

        _, assertion = self._make_used_ruleset_with_assertion()
        assertion.severity = Severity.WARNING
        with pytest.raises(ValidationError) as exc:
            assertion.clean()
        assert "severity" in exc.value.message_dict

    def test_clean_allows_message_template_change(self):
        """Improving the failure message text doesn't change rule logic."""
        _, assertion = self._make_used_ruleset_with_assertion()
        assertion.message_template = "Better wording here"
        assertion.clean()  # must not raise

    def test_clean_allows_order_change(self):
        """``order`` is UI display only."""
        _, assertion = self._make_used_ruleset_with_assertion()
        assertion.order = 99
        assertion.clean()

    def test_clean_blocks_adding_new_assertion_to_used_ruleset(self):
        """A brand-new assertion attached to a locked ruleset is also a mutation.

        The gate must fire even when ``self.pk`` is None — the
        ruleset's behavior is changing because a new rule is being
        added to it.
        """
        ruleset, _ = self._make_used_ruleset_with_assertion()
        new_assertion = RulesetAssertion(
            ruleset=ruleset,
            operator=AssertionOperator.GE,
            target_data_path="$.added",
            rhs={"value": 0},
        )
        with pytest.raises(ValidationError) as exc:
            new_assertion.clean()
        # The error is non-field-scoped (we don't know which field
        # to attribute "you added a new row" to).
        # Django wraps non-field errors under __all__.
        assert exc.value.messages, "Expected at least one error message"


class RulesetAssertionFreshAuthoringTests(TestCase):
    """The assertion gate is dormant for unused parent rulesets."""

    def test_clean_allows_rhs_change_on_unused_ruleset(self):
        """Fresh authoring: tweak operand freely."""
        ruleset = RulesetFactory()
        assertion = RulesetAssertionFactory(
            ruleset=ruleset,
            operator=AssertionOperator.LE,
            target_data_path="$.value",
            rhs={"value": 100},
        )
        assertion.rhs = {"value": 200}
        assertion.clean()

    def test_clean_allows_new_assertion_on_unused_ruleset(self):
        """Adding rules to a brand-new ruleset is the normal path."""
        ruleset = RulesetFactory()
        new_assertion = RulesetAssertion(
            ruleset=ruleset,
            operator=AssertionOperator.LE,
            target_data_path="$.new",
            rhs={"value": 1},
        )
        new_assertion.clean()
