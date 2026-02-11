from django.test import Client
from django.test import TestCase
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.tests.factories import CustomValidatorFactory
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


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
        if not step.ruleset_id:
            ruleset = RulesetFactory(org=workflow.org)
            step.ruleset = ruleset
            step.save(update_fields=["ruleset"])
        return step

    def _make_custom_validator_step(self, workflow):
        """Create a step with CUSTOM_VALIDATOR that supports assertions."""
        validator = ValidatorFactory(
            validation_type=ValidationType.CUSTOM_VALIDATOR,
            allow_custom_assertion_targets=True,
        )
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
                "target_catalog_entry": "output:facility_electric_demand_w",
                "operator": "le",
                "comparison_value": "1000",
                "severity": "ERROR",
                "when_expression": "",
                "message_template": "Too high",
            },
            headers={"hx-request": "true"},
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
        step = WorkflowStepFactory(
            workflow=workflow,
            validator=custom_validator.validator,
        )
        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Assertion Type", body)

    def test_move_assertion_single_stage(self):
        """Verify assertions can be reordered within a single stage."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_custom_validator_step(workflow)
        assert step.ruleset
        RulesetAssertionFactory(ruleset=step.ruleset, order=10)
        a2 = RulesetAssertionFactory(ruleset=step.ruleset, order=20)
        move_url = reverse(
            "workflows:workflow_step_assertion_move",
            kwargs={"pk": workflow.pk, "step_id": step.pk, "assertion_id": a2.pk},
        )
        resp = self.client.post(move_url, data={"direction": "up"})
        self.assertEqual(resp.status_code, 204)
        orders = list(
            step.ruleset.assertions.order_by("order").values_list("pk", flat=True),
        )
        self.assertEqual(orders[0], a2.pk)

    def test_move_assertion_respects_stages(self):
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_energyplus_step(workflow)
        assert step.ruleset
        input_entry = ValidatorCatalogEntryFactory(
            validator=step.validator,
            slug="input-signal",
            run_stage=CatalogRunStage.INPUT,
        )
        output_entry = ValidatorCatalogEntryFactory(
            validator=step.validator,
            slug="output-signal",
            run_stage=CatalogRunStage.OUTPUT,
        )
        RulesetAssertionFactory(
            ruleset=step.ruleset,
            order=10,
            target_catalog_entry=input_entry,
            target_field="",
        )
        a_output = RulesetAssertionFactory(
            ruleset=step.ruleset,
            order=20,
            target_catalog_entry=output_entry,
            target_field="",
        )
        move_url = reverse(
            "workflows:workflow_step_assertion_move",
            kwargs={"pk": workflow.pk, "step_id": step.pk, "assertion_id": a_output.pk},
        )
        # Try to move output "up" (should stay in output bucket, not jump before input)
        resp = self.client.post(move_url, data={"direction": "up"})
        self.assertEqual(resp.status_code, 204)
        ordered = [
            a.resolved_run_stage for a in step.ruleset.assertions.order_by("order")
        ]
        # input should still precede output
        self.assertEqual(ordered, [CatalogRunStage.INPUT, CatalogRunStage.OUTPUT])

    def test_custom_validator_supports_assertions(self):
        """Verify CUSTOM_VALIDATOR type supports assertion creation."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_custom_validator_step(workflow)
        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)

    def test_cel_expression_requires_known_signal(self):
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
                "assertion_type": "cel_expr",
                "cel_expression": "unknown_signal < 5",
                "severity": "ERROR",
                "message_template": "",
            },
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Unknown signal(s) referenced", body)

    def test_custom_validator_assertion_create_allows_custom_target(self):
        """Verify assertions can use custom target fields when validator allows."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_custom_validator_step(workflow)
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
            headers={"hx-request": "true"},
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
                "target_catalog_entry": "does-not-exist",
                "operator": "ge",
                "comparison_value": "10",
                "severity": "ERROR",
                "when_expression": "",
                "message_template": "Bad",
            },
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Unknown signal(s) referenced", body)

    def test_create_custom_target_when_validator_allows(self):
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            allow_custom_assertion_targets=True,
        )
        ValidatorCatalogEntryFactory(
            validator=validator,
            slug="facility_electric_demand_w",
        )
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
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 204)

    def test_step_update_redirects_to_assertions(self):
        workflow = WorkflowFactory()
        step = self._make_energyplus_step(workflow)
        _login_as_author(self.client, workflow)

        # Create a resource file for the weather dropdown
        resource_file = ValidatorResourceFile.objects.create(
            validator=step.validator,
            name="San Francisco TMY3",
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        url = reverse(
            "workflows:workflow_step_settings",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            url,
            data={
                "name": "Energy check",
                "description": "",
                "weather_file": str(resource_file.id),
                "run_simulation": True,
                "idf_checks": [],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("assertions", response["Location"])
