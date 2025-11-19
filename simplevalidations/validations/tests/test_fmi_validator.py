from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError

from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.models import FMUModel, Validator
from simplevalidations.validations.tests.factories import ValidatorFactory

pytestmark = pytest.mark.django_db


def test_system_fmi_validator_can_exist_without_fmu():
    validator = ValidatorFactory(validation_type=ValidationType.FMI, is_system=True)
    # System validators can be placeholders until an FMU is attached.
    validator.full_clean()


def test_org_fmi_validator_requires_fmu_asset():
    org = OrganizationFactory()
    validator = ValidatorFactory(
        validation_type=ValidationType.FMI,
        org=org,
        is_system=False,
    )
    with pytest.raises(ValidationError):
        validator.full_clean()


def test_org_fmi_validator_accepts_fmu_asset():
    org = OrganizationFactory()
    fmu_file = SimpleUploadedFile("demo.fmu", b"dummy-fmu", content_type="application/octet-stream")
    fmu = FMUModel.objects.create(
        org=org,
        name="Demo FMU",
        file=fmu_file,
    )
    validator = Validator(
        name="Org FMI",
        slug="org-fmi",
        validation_type=ValidationType.FMI,
        org=org,
        is_system=False,
        fmu_model=fmu,
    )
    validator.full_clean()
