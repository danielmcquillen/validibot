"""Model-layer tenant-integrity tests for workflow steps.

Forms already scope validator and ruleset choices to the active organization,
but services, imports, admin code, and management commands can write through
the ORM directly. These tests ensure those alternate paths cannot attach
another tenant's custom validation resources to a workflow.
"""

import pytest
from django.core.exceptions import ValidationError

from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.models import WorkflowStep
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def test_direct_create_rejects_custom_validator_from_other_org():
    """Direct ORM writes must not bypass custom-validator tenant ownership."""

    workflow_org = OrganizationFactory()
    other_org = OrganizationFactory()
    workflow = WorkflowFactory(org=workflow_org)
    validator = ValidatorFactory(org=other_org, is_system=False)

    with pytest.raises(ValidationError, match="workflow organization"):
        WorkflowStep.objects.create(
            workflow=workflow,
            validator=validator,
            order=10,
            name="Cross-tenant validator",
        )


def test_direct_create_rejects_ruleset_from_other_org():
    """A workflow step cannot import a private ruleset from another tenant."""

    workflow_org = OrganizationFactory()
    other_org = OrganizationFactory()
    workflow = WorkflowFactory(org=workflow_org)
    validator = ValidatorFactory(is_system=True)
    ruleset = RulesetFactory(
        org=other_org,
        ruleset_type=validator.validation_type,
    )

    with pytest.raises(ValidationError, match="workflow organization"):
        WorkflowStep.objects.create(
            workflow=workflow,
            validator=validator,
            ruleset=ruleset,
            order=10,
            name="Cross-tenant ruleset",
        )


def test_system_validator_remains_available_to_any_org():
    """System-owned validators have no tenant owner and remain shareable."""

    workflow = WorkflowFactory()
    validator = ValidatorFactory(is_system=True, org=None)

    step = WorkflowStep.objects.create(
        workflow=workflow,
        validator=validator,
        order=10,
        name="System validator",
    )

    assert step.workflow == workflow
    assert step.validator == validator
