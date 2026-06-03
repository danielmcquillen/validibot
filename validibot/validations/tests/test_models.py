"""Model-level validation tests for validation runs, findings, and validators.

These tests cover small invariants that are easy to regress outside the
end-to-end validator suites: finding/run alignment and the capability flags
that drive assertion availability in workflow authoring.
"""

import pytest
from django.core.exceptions import ValidationError

from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationFinding
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory


def test_validation_finding_auto_sets_run_from_step_run(db):
    step_run = ValidationStepRunFactory()
    finding = ValidationFinding(
        validation_step_run=step_run,
        validation_run=None,
        severity=Severity.ERROR,
        message="Auto alignment",
    )

    finding.save()

    assert finding.validation_run == step_run.validation_run


def test_validation_finding_rejects_mismatched_run(db):
    step_run = ValidationStepRunFactory()
    other_run = ValidationRunFactory()
    finding = ValidationFinding(
        validation_step_run=step_run,
        validation_run=other_run,
        severity=Severity.ERROR,
        message="Mismatch",
    )

    with pytest.raises(ValidationError):
        finding.save()


def test_validator_supports_assertions_defaults_false(db):
    """New validators default to supports_assertions=False."""
    validator = ValidatorFactory(
        validation_type=ValidationType.JSON_SCHEMA,
        supports_assertions=False,
    )
    assert validator.supports_assertions is False


def test_validator_supports_assertions_true_for_basic(db):
    """Basic validator factory sets supports_assertions=True."""
    validator = ValidatorFactory(validation_type=ValidationType.BASIC)
    assert validator.supports_assertions is True


def test_validator_supports_assertions_true_for_energyplus(db):
    """EnergyPlus validator factory sets supports_assertions=True."""
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    assert validator.supports_assertions is True


def test_validator_supports_assertions_true_for_schema_types(db):
    """Schema validators support business-rule assertions after schema checks."""
    for vtype in (ValidationType.JSON_SCHEMA, ValidationType.XML_SCHEMA):
        validator = ValidatorFactory(validation_type=vtype)
        assert validator.supports_assertions is True, f"{vtype} should be True"


def test_cel_assertion_with_long_expression_passes_full_clean(db):
    """A CEL assertion over 255 chars saves — ``target_data_path`` is a TextField.

    Why it matters: ``target_data_path`` doubles as the display source for CEL
    assertions (``target_display`` returns it for CEL_EXPRESSION), so the form
    stores the entire expression there. CEL expressions are valid up to 4096
    characters, but the column used to be ``CharField(max_length=255)``. A
    realistic SHACL allow-list — ``o.namespaces_present.all(ns, ns in [...9
    URIs...])`` — runs ~400 characters and so failed ``full_clean()`` with a
    "max 255 characters" error. Worse, that error landed on the *hidden* target
    field (the UI hides it for CEL assertions), so the author saw only a generic
    banner with no visible message. Widening the column to ``TextField`` removes
    the whole failure mode; this test pins the column to the form's contract.
    """
    expr = (
        "o.namespaces_present.all(ns, ns in ["
        + ", ".join(f'"http://example.org/ns/{i}#"' for i in range(20))
        + "])"
    )
    # Guard the premise: the expression must actually exceed the old 255 cap,
    # otherwise this test would pass even with the regression present.
    assert len(expr) > 255  # noqa: PLR2004

    assertion = RulesetAssertion(
        ruleset=RulesetFactory(),
        order=10,
        assertion_type=AssertionType.CEL_EXPRESSION,
        operator=AssertionOperator.CEL_EXPR,
        target_data_path=expr,
        rhs={"expr": expr},
        severity=Severity.ERROR,
    )
    # full_clean() is what the create/update service runs; it raised the
    # max-length error before the fix. It must now pass cleanly.
    assertion.full_clean()
    assertion.save()
    assertion.refresh_from_db()
    assert assertion.target_data_path == expr
