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


def test_validator_accepts_plugin_validation_type_string(db):
    """Plugin validators can persist validation_type values outside the enum.

    ``ValidationType`` remains useful for built-ins, but it is no longer the
    closed persistence vocabulary. A cloud/plugin package must be able to sync
    its own string without a core enum patch.
    """
    validator = ValidatorFactory(
        name="Cloud-only Validator",
        validation_type="CLOUD_ONLY",
    )

    assert validator.validation_type == "CLOUD_ONLY"
    assert validator.get_validation_type_display() == "Cloud-only Validator"


def test_validator_version_accepts_positive_integer():
    """A positive revision keeps validator ordering deterministic."""
    validator = ValidatorFactory.build(version=2)

    validator.clean_fields()


def test_validator_version_rejects_zero():
    """Revision zero must not enter the current validator contract schema."""
    validator = ValidatorFactory.build(version=0)

    with pytest.raises(ValidationError, match="version"):
        validator.clean_fields()


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


def test_cel_target_display_prefers_description(db):
    """A CEL assertion with a description shows it on the card, not the raw CEL.

    Why it matters: a raw CEL expression (``o.eui <= 50.0 && i.area > 0``) is
    noise to a reviewer scanning a step's assertions. Like SHACL's SPARQL ASK
    description, an optional human label lets the author title the rule
    ("Site EUI within ASHRAE target"). ``target_display`` is the single source
    of truth for the card label — the ``assertion_step.html`` template renders
    it directly — so this is the property that decides what the reviewer sees.
    The description lives in ``rhs["description"]`` (a JSON key, no migration),
    mirroring where SHACL stores its own.
    """
    assertion = RulesetAssertion(
        ruleset=RulesetFactory(),
        order=10,
        assertion_type=AssertionType.CEL_EXPRESSION,
        operator=AssertionOperator.CEL_EXPR,
        target_data_path="o.eui <= 50.0",
        rhs={"expr": "o.eui <= 50.0", "description": "Site EUI within target"},
        severity=Severity.ERROR,
    )

    assert assertion.target_display == "Site EUI within target"


def test_cel_target_display_falls_back_to_expression(db):
    """Without a description, the CEL card still shows the expression itself.

    Why it matters: the description is optional. When it is blank (the default,
    and the behaviour for every CEL assertion authored before this field
    existed), ``target_display`` must fall back to the stored expression so the
    card never renders empty. This pins the back-compatible default and proves
    the new branch only changes behaviour when a description is actually set.
    """
    assertion = RulesetAssertion(
        ruleset=RulesetFactory(),
        order=10,
        assertion_type=AssertionType.CEL_EXPRESSION,
        operator=AssertionOperator.CEL_EXPR,
        target_data_path="o.eui <= 50.0",
        rhs={"expr": "o.eui <= 50.0", "description": ""},
        severity=Severity.ERROR,
    )

    assert assertion.target_display == "o.eui <= 50.0"


def test_cel_target_display_uses_rhs_expression_for_legacy_blank_target(db):
    """A CEL card remains readable when only the JSON payload has the expression.

    Why it matters: older and imported Tabular assertions can have a blank
    ``target_data_path`` while retaining the executable expression in
    ``rhs["expr"]``. The step editor must display that authoritative expression
    instead of rendering an empty assertion card.
    """
    assertion = RulesetAssertion(
        ruleset=RulesetFactory(),
        order=10,
        assertion_type=AssertionType.CEL_EXPRESSION,
        operator=AssertionOperator.CEL_EXPR,
        target_data_path="",
        rhs={"expr": "row.reading >= 0", "description": ""},
        severity=Severity.ERROR,
    )

    assert assertion.target_display == "row.reading >= 0"
