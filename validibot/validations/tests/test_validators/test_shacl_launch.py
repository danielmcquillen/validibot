"""Tests for the Django-side SHACL input resolution (launch.py).

The SHACL container has no database, so everything it needs is resolved here and
shipped in the envelope. These tests pin that resolution:

- ``resolve_shacl_inputs`` merges library + step shapes, resolves the rdf_format,
  forwards the engine knobs + the deployment ``enable_advanced_features`` gate +
  the resource limits.
- ``resolve_sparql_ask_specs`` rehydrates only the SHACL (SPARQL-ASK)
  ``RulesetAssertion`` rows into typed specs, in order, skipping malformed rows —
  and leaves CEL/Basic rows for the Django-side pass.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.shacl.launch import resolve_shacl_inputs
from validibot.validations.validators.shacl.launch import resolve_sparql_ask_specs

pytestmark = pytest.mark.django_db

SHAPES = "@prefix sh: <http://www.w3.org/ns/shacl#> . # step shapes"


def _validator():
    return ValidatorFactory(validation_type=ValidationType.SHACL, is_system=False)


def _submission():
    return SubmissionFactory(
        content="@prefix ex: <http://example.org/> . ex:a a ex:Thing .",
        file_type=SubmissionFileType.TEXT,
    )


@override_settings(SHACL_ENABLE_ADVANCED_FEATURES=False)
def test_resolve_shacl_inputs_carries_shapes_and_knobs():
    """The step ruleset's shapes, format, and engine knobs reach SHACLInputs.

    The deployment gate is pinned off here to prove a step-level
    ``advanced_shacl=True`` does NOT, on its own, enable embedded SPARQL
    execution — that still requires the deployment ``SHACL_ENABLE_ADVANCED_FEATURES``
    setting (which this environment happens to enable by default).
    """
    ruleset = RulesetFactory(
        ruleset_type=RulesetType.SHACL,
        rules_text=SHAPES,
        metadata={
            "inference_mode": "owlrl",
            "advanced_shacl": True,
            "submission_format": "turtle",
            "shacl_result_handling": "report_only",
        },
    )
    inputs = resolve_shacl_inputs(
        validator=_validator(),
        ruleset=ruleset,
        submission=_submission(),
    )

    assert inputs.shapes_text == SHAPES
    assert inputs.rdf_format == "turtle"
    assert inputs.inference_mode == "owlrl"
    assert inputs.advanced_shacl is True
    assert inputs.shacl_result_handling == "report_only"
    # Deployment gate defaults off even when the step requested advanced.
    assert inputs.enable_advanced_features is False
    # Resource limits are populated (clamped defaults).
    assert inputs.max_data_triples > 0
    assert inputs.pyshacl_timeout_seconds > 0


def test_resolve_shacl_inputs_merges_library_default_then_step():
    """Library default_ruleset shapes come first, step shapes layer after."""
    default_ruleset = RulesetFactory(
        ruleset_type=RulesetType.SHACL,
        rules_text="# LIBRARY",
    )
    validator = _validator()
    validator.default_ruleset = default_ruleset
    validator.save(update_fields=["default_ruleset"])

    step_ruleset = RulesetFactory(ruleset_type=RulesetType.SHACL, rules_text="# STEP")

    inputs = resolve_shacl_inputs(
        validator=validator,
        ruleset=step_ruleset,
        submission=_submission(),
    )

    assert inputs.shapes_text.startswith("# LIBRARY")
    assert inputs.shapes_text.endswith("# STEP")


@override_settings(SHACL_ENABLE_ADVANCED_FEATURES=True)
def test_enable_advanced_features_forwarded_from_settings():
    """The deployment-level advanced gate is forwarded into the envelope."""
    ruleset = RulesetFactory(ruleset_type=RulesetType.SHACL, rules_text=SHAPES)
    inputs = resolve_shacl_inputs(
        validator=_validator(),
        ruleset=ruleset,
        submission=_submission(),
    )
    assert inputs.enable_advanced_features is True


def test_resolve_sparql_ask_specs_only_shacl_rows_in_order():
    """Only SHACL (SPARQL-ASK) rows become specs; CEL/Basic rows are left out."""
    ruleset = RulesetFactory(ruleset_type=RulesetType.SHACL, rules_text=SHAPES)
    # Two SHACL assertions (should be returned, in order)…
    RulesetAssertionFactory(
        ruleset=ruleset,
        order=20,
        assertion_type=AssertionType.SHACL,
        operator=AssertionOperator.SPARQL_ASK,
        target_data_path="shacl.data",
        severity=Severity.ERROR,
        rhs={
            "target_graph": "data",
            "query": "ASK { ?s a ?t }",
            "description": "second",
        },
    )
    RulesetAssertionFactory(
        ruleset=ruleset,
        order=10,
        assertion_type=AssertionType.SHACL,
        operator=AssertionOperator.SPARQL_ASK,
        target_data_path="shacl.results",
        severity=Severity.WARNING,
        rhs={
            "target_graph": "results",
            "query": "ASK { ?s ?p ?o }",
            "description": "first",
        },
    )
    # …and one Basic assertion (must NOT appear — it runs in the Django pass).
    RulesetAssertionFactory(
        ruleset=ruleset,
        order=30,
        assertion_type=AssertionType.BASIC,
        operator=AssertionOperator.EQ,
        target_data_path="shacl_violation_count",
        rhs={"value": 0},
    )

    specs = resolve_sparql_ask_specs(_validator(), ruleset)

    expected_shacl_spec_count = 2  # two SHACL rows; the Basic row is excluded
    assert len(specs) == expected_shacl_spec_count
    # Ordered by the assertion `order` field (10 before 20).
    assert specs[0].description == "first"
    assert specs[0].target_graph == "results"
    assert specs[0].severity == Severity.WARNING
    assert specs[1].description == "second"
    # assertion_id is carried for finding attribution back in Django.
    assert all(s.assertion_id is not None for s in specs)


def test_resolve_sparql_ask_specs_skips_invalid_rows():
    """Rows with an empty query or bad target_graph are dropped, not shipped.

    The form validates these at save time; this guards fixtures / imports that
    bypass the form so a malformed row can't reach the container.
    """
    ruleset = RulesetFactory(ruleset_type=RulesetType.SHACL, rules_text=SHAPES)
    RulesetAssertionFactory(
        ruleset=ruleset,
        assertion_type=AssertionType.SHACL,
        operator=AssertionOperator.SPARQL_ASK,
        target_data_path="shacl.data",
        severity=Severity.ERROR,
        rhs={"target_graph": "data", "query": "   "},  # empty query
    )
    assert resolve_sparql_ask_specs(_validator(), ruleset) == []
