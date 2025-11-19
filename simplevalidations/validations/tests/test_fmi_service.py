from __future__ import annotations

import io
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.validations.constants import CatalogRunStage, ValidationType
from simplevalidations.validations.services.fmi import create_fmi_validator

pytestmark = pytest.mark.django_db


def _make_fake_fmu(name: str = "demo") -> SimpleUploadedFile:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="2.0" modelName="{name}">
  <ModelVariables>
    <ScalarVariable name="u_in" causality="input" valueReference="1"><Real/></ScalarVariable>
    <ScalarVariable name="y_out" causality="output" valueReference="2"><Real/></ScalarVariable>
  </ModelVariables>
</fmiModelDescription>
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("modelDescription.xml", xml)
    buf.seek(0)
    return SimpleUploadedFile(f"{name}.fmu", buf.getvalue(), content_type="application/octet-stream")


def test_create_fmi_validator_introspects_and_seeds_catalog():
    org = OrganizationFactory()
    project = ProjectFactory(org=org)
    upload = _make_fake_fmu()

    validator = create_fmi_validator(org=org, project=project, name="Test FMU", upload=upload)

    assert validator.validation_type == ValidationType.FMI
    assert validator.catalog_entries.filter(run_stage=CatalogRunStage.INPUT).count() == 1
    assert validator.catalog_entries.filter(run_stage=CatalogRunStage.OUTPUT).count() == 1
    fmu_model = validator.fmu_model
    assert fmu_model is not None
    assert fmu_model.variables.count() == 2
    assert fmu_model.is_approved is True
