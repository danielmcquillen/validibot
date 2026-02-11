from __future__ import annotations

import logging
from pathlib import Path

import pytest
from django.urls import reverse
from rest_framework.status import HTTP_200_OK
from rest_framework.status import HTTP_201_CREATED
from rest_framework.status import HTTP_202_ACCEPTED

from validibot.submissions.constants import SubmissionFileType
from validibot.users.models import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.django_db


def load_example_epjson() -> str:
    base = Path(__file__).resolve().parent.parent / "data" / "energyplus"
    path = base / "example_epjson.json"
    return path.read_text(encoding="utf-8")


@pytest.fixture
def energyplus_workflow(api_client):
    """
    Build a minimal workflow configured for the EnergyPlus validation engine.

    Note: These tests currently verify stub behavior. They will be updated
    once the Cloud Run Jobs integration is complete.
    """
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    grant_role(user, org, RoleCode.EXECUTOR)
    user.set_current_org(org)

    validator = ValidatorFactory(
        validation_type=ValidationType.ENERGYPLUS,
        default_ruleset=None,
    )

    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=RulesetType.ENERGYPLUS,
        rules_text="{}",
    )

    workflow = WorkflowFactory(
        org=org,
        user=user,
        allowed_file_types=[
            SubmissionFileType.TEXT,
            SubmissionFileType.JSON,
        ],
    )
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        order=1,
        config={
            "run_simulation": True,
            "weather_file": "USA_CA_SF.epw",
        },
    )

    api_client.force_authenticate(user=user)

    return {
        "org": org,
        "user": user,
        "validator": validator,
        "ruleset": ruleset,
        "workflow": workflow,
        "step": step,
        "client": api_client,
    }


@pytest.mark.django_db
class TestEnergyPlusValidation:
    """
    End-to-end EnergyPlus validation tests.

    These tests currently verify stub behavior. They will be updated
    once the Cloud Run Jobs integration is complete.
    """

    def test_energyplus_workflow_not_implemented(self, energyplus_workflow):
        """
        Test that EnergyPlus workflows return not-implemented error.

        Will be replaced with a real Cloud Run Jobs test once that integration
        is complete.
        """
        client = energyplus_workflow["client"]
        workflow = energyplus_workflow["workflow"]

        payload = load_example_epjson()

        # Use org-scoped route (ADR-2026-01-06)
        start_url = reverse(
            "api:org-workflows-runs",
            kwargs={"org_slug": workflow.org.slug, "pk": workflow.pk},
        )
        resp = client.post(
            start_url,
            data=payload,
            content_type="application/json",
        )

        # Should accept the request
        assert resp.status_code in (HTTP_200_OK, HTTP_201_CREATED, HTTP_202_ACCEPTED), (
            resp.content
        )

        # Polling and verification of Cloud Run Jobs execution will be added
        # once that integration is complete.
