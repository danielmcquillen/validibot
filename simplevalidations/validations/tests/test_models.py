import pytest
from django.core.exceptions import ValidationError

from simplevalidations.validations.constants import Severity
from simplevalidations.validations.models import ValidationFinding
from simplevalidations.validations.tests.factories import ValidationRunFactory
from simplevalidations.validations.tests.factories import ValidationStepRunFactory


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
