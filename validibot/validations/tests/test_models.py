import pytest
from django.core.exceptions import ValidationError

from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidationFinding
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


def test_validator_supports_assertions_false_for_schema_types(db):
    """Schema-only validators do not support assertions."""
    for vtype in (ValidationType.JSON_SCHEMA, ValidationType.XML_SCHEMA):
        validator = ValidatorFactory(validation_type=vtype)
        assert validator.supports_assertions is False, f"{vtype} should be False"
