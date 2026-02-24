from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import ValidationType
from validibot.validations.models import FMUModel
from validibot.validations.models import Validator
from validibot.validations.tests.factories import ValidatorFactory


class FMIValidatorModelTests(TestCase):
    """
    Validate FMU attachment requirements for FMU validators.

    FMU validators owned by an org must carry an FMU; system FMU validators can
    exist as placeholders until an FMU is assigned.
    """

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    def test_system_fmi_validator_can_exist_without_fmu(self) -> None:
        """System FMU validators remain valid without an FMU attachment."""

        validator = ValidatorFactory(validation_type=ValidationType.FMU, is_system=True)
        validator.full_clean()

    def test_org_fmi_validator_requires_fmu_asset(self) -> None:
        """
        Non-system FMU validators must reference an FMUModel before validation.
        """

        # Build without saving so we can assert full_clean fails as expected.
        validator = ValidatorFactory.build(
            validation_type=ValidationType.FMU,
            org=self.org,
            is_system=False,
        )
        with pytest.raises(ValidationError):
            validator.full_clean()

    def test_org_fmi_validator_accepts_fmu_asset(self) -> None:
        """Org FMU validators validate successfully when an FMUModel is attached."""

        fmu_file = SimpleUploadedFile(
            "demo.fmu",
            b"dummy-fmu",
            content_type="application/octet-stream",
        )
        fmu = FMUModel.objects.create(
            org=self.org,
            name="Demo FMU",
            file=fmu_file,
        )
        validator = Validator(
            name="Org FMI",
            slug="org-fmi",
            validation_type=ValidationType.FMU,
            org=self.org,
            is_system=False,
            fmu_model=fmu,
        )
        validator.full_clean()
