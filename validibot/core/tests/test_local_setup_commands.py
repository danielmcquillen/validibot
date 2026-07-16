"""Regression tests for code-backed data used by local development.

Local startup synchronizes the validator catalog before seeding bundled
EnergyPlus weather files. Validator version history is intentionally retained,
so setup commands must bind new resources and workflows to the exact contract
declared by the running code rather than assuming one row per validation type.
These tests preserve that contract as validator versions accumulate.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command

from validibot.core.management.commands.seed_weather_files import WEATHER_FILES
from validibot.core.management.commands.setup_e2e_workflows import EP_WORKFLOW_SLUG
from validibot.core.management.commands.setup_e2e_workflows import Command
from validibot.projects.tests.factories import ProjectFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.energyplus.config import (
    config as energyplus_config,
)
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _energyplus_validator(*, version: int):
    """Create one historical or current row in the EnergyPlus family."""
    return ValidatorFactory(
        slug=energyplus_config.slug,
        name=f"EnergyPlus contract v{version}",
        validation_type=ValidationType.ENERGYPLUS,
        version=version,
        is_system=True,
    )


def test_seed_weather_files_uses_current_energyplus_contract(tmp_path):
    """Weather seeding must tolerate retained validator version history.

    A version bump leaves the old row in place for existing workflows. Local
    startup must therefore complete without ``MultipleObjectsReturned`` and
    attach newly seeded files only to the contract shipped by the running code.
    """
    historical = _energyplus_validator(version=energyplus_config.version - 1)
    current = _energyplus_validator(version=energyplus_config.version)
    filename, _display_name = WEATHER_FILES[0]
    (tmp_path / filename).write_bytes(b"minimal EPW fixture")

    call_command("seed_weather_files", source_dir=str(tmp_path))
    call_command("seed_weather_files", source_dir=str(tmp_path))

    resources = ValidatorResourceFile.objects.filter(filename=filename)
    assert resources.count() == 1
    assert resources.get().validator == current
    assert not resources.filter(validator=historical).exists()


def test_setup_e2e_workflow_uses_current_energyplus_contract():
    """New E2E workflows must never bind to a retained historical contract.

    Default model ordering does not represent contract currency. Pinning this
    selection protects the generated workflow, its weather resource, and its
    assertions from silently using stale step I/O definitions.
    """
    historical = _energyplus_validator(version=energyplus_config.version - 1)
    current = _energyplus_validator(version=energyplus_config.version)
    current_weather = ValidatorResourceFile.objects.create(
        validator=current,
        org=None,
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        name="Current weather",
        filename="current.epw",
        file=ContentFile(b"weather", name="current.epw"),
    )
    ValidatorResourceFile.objects.create(
        validator=historical,
        org=None,
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        name="Historical weather",
        filename="historical.epw",
        file=ContentFile(b"old weather", name="historical.epw"),
    )
    org = OrganizationFactory()
    user = UserFactory()
    project = ProjectFactory(org=org)
    WorkflowFactory(
        org=org,
        user=user,
        project=project,
        slug=EP_WORKFLOW_SLUG,
        version=1,
    )
    command = Command()
    step = SimpleNamespace(ruleset=object())
    command._create_energyplus_step = Mock(return_value=step)
    command._attach_template_idf = Mock()
    command._attach_weather_file = Mock()
    command._create_output_assertions = Mock()

    workflow = command._ensure_energyplus_template_workflow(org, user, project)

    assert workflow is not None
    assert command._create_energyplus_step.call_args.args[1] == current
    command._attach_weather_file.assert_called_once_with(step, current_weather)
    assert command._create_output_assertions.call_args.args[1] == current
