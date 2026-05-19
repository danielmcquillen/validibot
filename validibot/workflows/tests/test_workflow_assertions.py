"""Tests for workflow-step assertion authoring and assertion-stage behavior.

These cover the modal used by workflow authors to create assertions, the
server-side persistence of assertion rows, and the step editor surfaces that
show validator signals used by those assertions.
"""

from django.test import Client
from django.test import TestCase
from django.test import override_settings
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
from validibot.validations.tests.factories import SignalDefinitionFactory
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
        SignalDefinitionFactory(
            validator=validator,
            contract_key="floor_area",
            direction="input",
        )
        SignalDefinitionFactory(
            validator=validator,
            contract_key="facility_electric_demand_w",
            direction="output",
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

    def _make_shacl_step(self, workflow):
        """Create a SHACL step with output signal definitions for assertions."""
        validator = ValidatorFactory(
            validation_type=ValidationType.SHACL,
            supports_assertions=True,
        )
        SignalDefinitionFactory(
            validator=validator,
            contract_key="shacl_violation_count",
            label="SHACL Violation Count",
            direction="output",
        )
        SignalDefinitionFactory(
            validator=validator,
            contract_key="triple_count",
            label="Triple Count",
            direction="output",
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
        SignalDefinitionFactory(
            validator=custom_validator.validator,
            contract_key="custom-signal",
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

    def test_shacl_assertion_modal_shows_shacl_type_first(self):
        """SHACL steps should expose SHACL SPARQL assertions as the first type."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_shacl_step(workflow)

        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('value="shacl"', body)
        self.assertLess(body.index('value="shacl"'), body.index('value="basic"'))

    def test_non_shacl_assertion_modal_hides_shacl_type(self):
        """Non-SHACL validators should not show the SHACL assertion type."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_energyplus_step(workflow)

        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('value="shacl"', response.content.decode())

    def test_create_shacl_sparql_assertion(self):
        """A SHACL assertion post creates a SPARQL ASK RulesetAssertion row."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_shacl_step(workflow)

        create_url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            create_url,
            data={
                "assertion_type": AssertionType.SHACL,
                "shacl_description": "Graph is not empty",
                "shacl_target_graph": "data",
                "shacl_query": "ASK { ?s ?p ?o }",
                "severity": "ERROR",
                "when_expression": "o.triple_count > 0",
                "message_template": "No RDF triples were found.",
            },
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 204)
        assertion = step.ruleset.assertions.get(assertion_type=AssertionType.SHACL)
        self.assertEqual(assertion.operator, AssertionOperator.SPARQL_ASK)
        self.assertEqual(assertion.target_data_path, "shacl.data")
        self.assertEqual(assertion.rhs["query"], "ASK { ?s ?p ?o }")
        self.assertEqual(assertion.rhs["description"], "Graph is not empty")
        self.assertEqual(assertion.message_template, "No RDF triples were found.")
        self.assertEqual(assertion.when_expression, "")

    @override_settings(SHACL_SPARQL_QUERY_LENGTH_MAX=64)
    def test_shacl_assertion_modal_sets_query_maxlength(self):
        """The SHACL query textarea should expose the configured length cap."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_shacl_step(workflow)

        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('name="shacl_query"', body)
        self.assertIn('maxlength="64"', body)

    def test_shacl_sparql_assertion_rejects_non_ask_queries(self):
        """Form validation should reject every non-ASK SPARQL form."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_shacl_step(workflow)

        create_url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        queries = {
            "select": "SELECT * WHERE { ?s ?p ?o }",
            "construct": "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
            "describe": "DESCRIBE ?s WHERE { ?s ?p ?o }",
            "insert": "INSERT DATA { <urn:s> <urn:p> <urn:o> }",
        }
        for query_type, query in queries.items():
            with self.subTest(query_type=query_type):
                response = self.client.post(
                    create_url,
                    data={
                        "assertion_type": AssertionType.SHACL,
                        "shacl_target_graph": "data",
                        "shacl_query": query,
                        "severity": "ERROR",
                    },
                    headers={"hx-request": "true"},
                )

                self.assertEqual(response.status_code, 200)
                self.assertIn("ASK", response.content.decode())

        self.assertEqual(step.ruleset.assertions.count(), 0)

    @override_settings(SHACL_SPARQL_QUERY_LENGTH_MAX=20)
    def test_shacl_sparql_assertion_rejects_oversized_query(self):
        """Server-side validation should enforce the configured query size."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_shacl_step(workflow)

        create_url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            create_url,
            data={
                "assertion_type": AssertionType.SHACL,
                "shacl_target_graph": "data",
                "shacl_query": "ASK { ?s ?p ?o . ?s ?p ?o }",
                "severity": "ERROR",
            },
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "maximum length of 20 characters",
            response.content.decode(),
        )
        self.assertEqual(step.ruleset.assertions.count(), 0)

    @override_settings(SHACL_SPARQL_ASKS_PER_STEP_MAX=1)
    def test_shacl_sparql_assertion_count_cap_is_enforced(self):
        """The per-step SPARQL assertion cap should apply to row-based ASKs."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_shacl_step(workflow)
        RulesetAssertionFactory(
            ruleset=step.ruleset,
            assertion_type=AssertionType.SHACL,
            operator=AssertionOperator.SPARQL_ASK,
            target_data_path="shacl.data",
            rhs={
                "target_graph": "data",
                "query": "ASK { ?s ?p ?o }",
                "description": "Existing",
            },
        )

        create_url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            create_url,
            data={
                "assertion_type": AssertionType.SHACL,
                "shacl_target_graph": "data",
                "shacl_query": "ASK { ?s ?p ?o }",
                "severity": "ERROR",
            },
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "already has 1 SHACL SPARQL assertions", response.content.decode()
        )
        self.assertEqual(
            step.ruleset.assertions.filter(assertion_type=AssertionType.SHACL).count(),
            1,
        )

    def test_shacl_step_editor_shows_output_signals(self):
        """SHACL output signals should appear in the step editor right column."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_shacl_step(workflow)

        url = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("o.shacl_violation_count", body)
        self.assertIn("o.triple_count", body)

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
        input_sig = SignalDefinitionFactory(
            validator=step.validator,
            contract_key="input-signal",
            direction="input",
        )
        output_sig = SignalDefinitionFactory(
            validator=step.validator,
            contract_key="output-signal",
            direction="output",
        )
        RulesetAssertionFactory(
            ruleset=step.ruleset,
            order=10,
            target_signal_definition=input_sig,
            target_data_path="",
        )
        a_output = RulesetAssertionFactory(
            ruleset=step.ruleset,
            order=20,
            target_signal_definition=output_sig,
            target_data_path="",
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

    def test_basic_validator_supports_assertions(self):
        """Basic Validator should support step-level assertions."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            supports_assertions=True,
        )
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        if not step.ruleset_id:
            ruleset = RulesetFactory(org=workflow.org)
            step.ruleset = ruleset
            step.save(update_fields=["ruleset"])
        url = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Add assertion", response.content.decode())

    def test_schema_validator_hides_assertions(self):
        """JSON Schema validator should not show assertion UI."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(
            validation_type=ValidationType.JSON_SCHEMA,
            supports_assertions=False,
        )
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        url = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Add assertion", response.content.decode())

    def test_cel_expression_requires_namespace_prefix(self):
        """Bare identifiers (without a namespace prefix like s. or p.)
        should be rejected by the CEL expression validator."""
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
        self.assertIn("Bare identifiers are not allowed", body)

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
                "target_data_path": "payload.meta.score",
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
        self.assertIn("for signals", body)

    def test_create_custom_target_when_validator_allows(self):
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            allow_custom_assertion_targets=True,
        )
        SignalDefinitionFactory(
            validator=validator,
            contract_key="facility_electric_demand_w",
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
                "target_data_path": "metrics.custom.value",
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
                "validation_mode": "direct",
                "weather_file": str(resource_file.id),
                "run_simulation": True,
                "idf_checks": [],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("assertions", response["Location"])
