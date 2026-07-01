"""Tests for the community workflow-definition contract projection.

ADR-2026-06-18 (implementation note). The single projection
(``build_workflow_definition_contract`` / ``compute_workflow_definition_hash``)
is the source of truth for "what, semantically, is this workflow?" — the thing
the evidence manifest and the Pro credential will both hash.

These are the ADR's **anti-drift guards**: they pin the semantic/cosmetic
boundary so a cosmetic edit can never move the hash while a semantic edit always
does. Getting this wrong is exactly the class of bug the single-projection work
exists to prevent (a display tweak silently invalidating a prior attestation).
"""

from __future__ import annotations

from django.test import TestCase

from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.models import RulesetAssertion
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.constants import WorkflowConstantType
from validibot.workflows.models import WorkflowConstant
from validibot.workflows.models import WorkflowSignalMapping
from validibot.workflows.services.contract_snapshot import (
    build_workflow_definition_contract,
)
from validibot.workflows.services.contract_snapshot import (
    compute_workflow_definition_hash,
)
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


class ContractProjectionTests(TestCase):
    """The projection covers constants + signal definitions + steps."""

    def setUp(self):
        self.workflow = WorkflowFactory()
        self.validator = ValidatorFactory(validation_type=ValidationType.BASIC)
        self.ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        self.step = WorkflowStepFactory(
            workflow=self.workflow,
            validator=self.validator,
            ruleset=self.ruleset,
            order=10,
        )

    def test_contract_includes_constants_with_exact_value(self):
        """A NUMBER constant's exact decimal string appears in the preimage.

        The attested precision (``0.40``, not ``0.4``) must be what gets
        hashed, or the credential's claim and the hash would disagree.
        """
        WorkflowConstant.objects.create(
            workflow=self.workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        contract = build_workflow_definition_contract(self.workflow)
        assert contract["constants"] == [
            {"name": "energy_price", "data_type": "NUMBER", "value": "0.40"},
        ]

    def test_contract_includes_signal_mapping_definitions(self):
        """Signal-mapping *definitions* (not resolved values) are in the preimage.

        The workflow-defined config is safe to hash; the resolved ``s.*`` value
        is submission-derived and must never appear here.
        """
        WorkflowSignalMapping.objects.create(
            workflow=self.workflow,
            name="reported_total",
            source_path="$.total",
        )
        contract = build_workflow_definition_contract(self.workflow)
        assert contract["signal_mappings"][0]["name"] == "reported_total"
        assert contract["signal_mappings"][0]["source_path"] == "$.total"

    def test_constants_sorted_by_name_not_position(self):
        """Constants project in name order, independent of display position.

        Sorting by name (not position/pk) is what makes the hash stable across a
        cosmetic reorder and across export/import.
        """
        WorkflowConstant.objects.create(
            workflow=self.workflow,
            name="zulu",
            data_type=WorkflowConstantType.STRING,
            value="z",
            position=10,
        )
        WorkflowConstant.objects.create(
            workflow=self.workflow,
            name="alpha",
            data_type=WorkflowConstantType.STRING,
            value="a",
            position=20,
        )
        contract = build_workflow_definition_contract(self.workflow)
        names = [c["name"] for c in contract["constants"]]
        assert names == ["alpha", "zulu"]


class HashDriftTests(TestCase):
    """The hash moves on semantic edits and stays put on cosmetic ones."""

    def setUp(self):
        self.workflow = WorkflowFactory()
        self.validator = ValidatorFactory(validation_type=ValidationType.BASIC)
        self.ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        WorkflowStepFactory(
            workflow=self.workflow,
            validator=self.validator,
            ruleset=self.ruleset,
            order=10,
        )
        self.constant = WorkflowConstant.objects.create(
            workflow=self.workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )

    def test_editing_constant_value_changes_hash(self):
        """Changing a constant's value re-bases the hash (semantic edit).

        The core trust property: a receipt that passed against ``0.40`` must not
        silently be treated as passing against ``0.45`` under the same hash.
        """
        before = compute_workflow_definition_hash(self.workflow)
        # Bypass the versioned edit-after-runs guard (no runs here anyway) by a
        # direct field update — we're testing the projection, not the guard.
        WorkflowConstant.objects.filter(pk=self.constant.pk).update(value="0.45")
        after = compute_workflow_definition_hash(self.workflow)
        assert before != after

    def test_editing_constant_description_does_not_change_hash(self):
        """A cosmetic ``description`` edit must NOT change the hash.

        Description is not a semantic field; hashing it would make every doc
        tweak invalidate prior attestations — the exact drift bug we prevent.
        """
        before = compute_workflow_definition_hash(self.workflow)
        WorkflowConstant.objects.filter(pk=self.constant.pk).update(
            description="clarified wording",
        )
        after = compute_workflow_definition_hash(self.workflow)
        assert before == after

    def test_reordering_constant_position_does_not_change_hash(self):
        """Reordering (position) is cosmetic and must not move the hash."""
        WorkflowConstant.objects.create(
            workflow=self.workflow,
            name="max_eui",
            data_type=WorkflowConstantType.NUMBER,
            value="120",
        )
        before = compute_workflow_definition_hash(self.workflow)
        # Swap positions — same set of constants, different display order.
        for c in self.workflow.constants.all():
            WorkflowConstant.objects.filter(pk=c.pk).update(position=c.position + 100)
        after = compute_workflow_definition_hash(self.workflow)
        assert before == after

    def test_editing_assertion_message_does_not_change_hash(self):
        """A cosmetic assertion ``message_template`` edit must NOT change the hash.

        This is the Pro-inherited bug the single projection fixes: message text
        is mutable/cosmetic (excluded from ``IMMUTABLE_ASSERTION_FIELDS``), so it
        must not feed the digest.
        """
        assertion = RulesetAssertion.objects.create(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.EQ,
            rhs={"expr": "payload.price == c.energy_price"},
            message_template="Original message.",
        )
        before = compute_workflow_definition_hash(self.workflow)
        RulesetAssertion.objects.filter(pk=assertion.pk).update(
            message_template="Reworded message.",
        )
        after = compute_workflow_definition_hash(self.workflow)
        assert before == after

    def test_editing_assertion_expression_changes_hash(self):
        """Changing an assertion's CEL expression (rhs) re-bases the hash.

        ``rhs`` is a semantic field — the actual check — so an edit must be
        reflected in the digest.
        """
        assertion = RulesetAssertion.objects.create(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.EQ,
            rhs={"expr": "payload.price == c.energy_price"},
        )
        before = compute_workflow_definition_hash(self.workflow)
        RulesetAssertion.objects.filter(pk=assertion.pk).update(
            rhs={"expr": "payload.price > c.energy_price"},
        )
        after = compute_workflow_definition_hash(self.workflow)
        assert before != after
