import pytest
from django.urls import reverse

from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.tests.factories import (
    ValidatorCatalogEntryFactory,
    ValidatorFactory,
)
from simplevalidations.workflows.tests.factories import WorkflowFactory, WorkflowStepFactory


@pytest.mark.django_db
class TestWorkflowStepAssertions:
    def _login(self, client, workflow):
        user = workflow.user
        client.force_login(user)
        session = client.session
        session["active_org_id"] = workflow.org_id
        session.save()
        return user

    def _make_energyplus_step(self, workflow):
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        ValidatorCatalogEntryFactory(validator=validator, slug="facility_electric_demand_w")
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        return step

    def test_assertions_page_renders(self, client):
        workflow = WorkflowFactory()
        self._login(client, workflow)
        step = self._make_energyplus_step(workflow)
        url = reverse(
            "workflows:workflow_step_assertions",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = client.get(url)
        assert response.status_code == 200
        assert "Add assertion" in response.content.decode()

    def test_create_assertion(self, client):
        workflow = WorkflowFactory()
        self._login(client, workflow)
        step = self._make_energyplus_step(workflow)
        create_url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = client.post(
            create_url,
            data={
                "assertion_type": "threshold_max",
                "target_slug": "facility_electric_demand_w",
                "threshold_value": "1000",
                "severity": "ERROR",
                "when_expression": "",
                "message_template": "Too high",
            },
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 204
        step.refresh_from_db()
        assert step.ruleset.assertions.count() == 1

    def test_step_update_redirects_to_assertions(self, client):
        workflow = WorkflowFactory()
        step = self._make_energyplus_step(workflow)
        user = self._login(client, workflow)
        url = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = client.post(
            url,
            data={
                "name": "Energy check",
                "description": "",
                "run_simulation": True,
                "idf_checks": [],
                "simulation_checks": [],
            },
        )
        assert response.status_code == 302
        assert "assertions" in response["Location"]
