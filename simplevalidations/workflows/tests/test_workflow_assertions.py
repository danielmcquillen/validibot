import json

from django.test import Client, TestCase
from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.users.tests.utils import ensure_all_roles_exist
from simplevalidations.validations.constants import (
    AssertionOperator,
    AssertionType,
    CatalogRunStage,
    ValidationType,
)
from simplevalidations.validations.tests.factories import (
    CustomValidatorFactory,
    RulesetAssertionFactory,
    RulesetFactory,
    ValidatorCatalogEntryFactory,
    ValidatorFactory,
)
from simplevalidations.workflows.tests.factories import WorkflowFactory, WorkflowStepFactory


def _login_as_author(client: Client, workflow):
    """Log in as the workflow.user with author permissions in the org."""
    membership = workflow.user.memberships.get(org=workflow.org)
    membership.set_roles({RoleCode.AUTHOR})
    workflow.user.set_current_org(workflow.org)
    client.force_login(workflow.user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()
    return workflow.user


class WorkflowStepAssertionsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def _make_energyplus_step(self, workflow):
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        ValidatorCatalogEntryFactory(
            validator=validator,
            slug="floor_area",
            run_stage=CatalogRunStage.INPUT,
        )
        ValidatorCatalogEntryFactory(
            validator=validator,
            slug="facility_electric_demand_w",
            run_stage=CatalogRunStage.OUTPUT,
        )
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        return step

    def _make_basic_step(self, workflow):
        validator = ValidatorFactory(validation_type=ValidationType.BASIC)
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        if not step.ruleset_id:
            ruleset = RulesetFactory(org=workflow.org)
            step.ruleset = ruleset
            step.save(update_fields=["ruleset"])
        return step

    def test_assertions_page_renders(self):
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_energyplus_step(workflow)
        url = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Add assertion", response.content.decode())

    def test_create_assertion(self):
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_energyplus_step(workflow)
        create_url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            create_url,
            data={
                "assertion_type": "basic",
                "target_field": "facility_electric_demand_w",
                "operator": "le",
                "comparison_value": "1000",
                "severity": "ERROR",
                "when_expression": "",
                "message_template": "Too high",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 204)
        step.refresh_from_db()
        self.assertEqual(step.ruleset.assertions.count(), 1)

    def test_custom_validator_assertion_modal_renders(self):
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        custom_validator = CustomValidatorFactory(org=workflow.org)
        ValidatorCatalogEntryFactory(
            validator=custom_validator.validator,
            slug="custom-signal",
            label="Custom signal",
        )
        step = WorkflowStepFactory(workflow=workflow, validator=custom_validator.validator)
        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Assertion Type", body)
        self.assertIn("custom-signal", body)

    def test_basic_validator_supports_assertions(self):
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_basic_step(workflow)
        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)

    def test_basic_assertion_create_allows_custom_target(self):
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_basic_step(workflow)
        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            url,
            data={
                "assertion_type": "basic",
                "target_field": "payload.meta.score",
                "operator": AssertionOperator.GE,
                "comparison_value": "0.8",
                "severity": "WARNING",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 204)

    def test_custom_assertion_create_rejects_unknown_signal(self):
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_energyplus_step(workflow)
        create_url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            create_url,
            data={
                "assertion_type": AssertionType.BASIC,
                "target_field": "does-not-exist",
                "operator": "ge",
                "comparison_value": "10",
                "severity": "ERROR",
                "when_expression": "",
                "message_template": "Bad",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("available catalog targets", body)

    def test_create_custom_target_when_validator_allows(self):
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            allow_custom_assertion_targets=True,
        )
        ValidatorCatalogEntryFactory(validator=validator, slug="facility_electric_demand_w")
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        create_url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            create_url,
            data={
                "assertion_type": "basic",
                "target_field": "metrics.custom.value",
                "operator": "ge",
                "comparison_value": "42",
                "severity": "ERROR",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 204)

    def test_step_update_redirects_to_assertions(self):
        workflow = WorkflowFactory()
        step = self._make_energyplus_step(workflow)
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_step_settings",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            url,
            data={
                "name": "Energy check",
                "description": "",
                "run_simulation": True,
                "idf_checks": [],
                "simulation_checks": [],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("assertions", response["Location"])
