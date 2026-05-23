"""Tests for workflow-step assertion authoring and assertion-stage behavior.

These cover the modal used by workflow authors to create assertions, the
server-side persistence of assertion rows, and the step editor surfaces that
show validator signals used by those assertions.
"""

from django.test import Client
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from lxml import html as lxml_html

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
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.constants import WorkflowHistoryPolicy
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

    def assert_cel_expression_field_uses_full_width_modal_layout(self, body):
        """Assert CEL authoring has the same horizontal room as other large fields."""
        document = lxml_html.fromstring(body)
        cel_textareas = document.xpath('//textarea[@name="cel_expression"]')
        self.assertEqual(len(cel_textareas), 1)
        wrapper = cel_textareas[0]
        while wrapper is not None and wrapper.get("id") != "div_id_cel_expression":
            wrapper = wrapper.getparent()
        self.assertIsNotNone(wrapper)

        for ancestor in wrapper.iterancestors():
            ancestor_classes = set((ancestor.get("class") or "").split())
            if {"col-lg-3", "col-lg-9"} & ancestor_classes:
                self.fail(
                    "CEL expression field is still constrained by a Bootstrap "
                    f"grid column: {sorted(ancestor_classes)}",
                )
            if ancestor.tag == "form":
                break

    def _make_energyplus_step(self, workflow):
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        StepIODefinitionFactory(
            validator=validator,
            contract_key="floor_area",
            direction="input",
        )
        StepIODefinitionFactory(
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
        StepIODefinitionFactory(
            validator=validator,
            contract_key="shacl_violation_count",
            label="SHACL Violation Count",
            direction="output",
        )
        StepIODefinitionFactory(
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

    def test_create_assertion_blocked_on_locked_versioned_workflow(self):
        """Versioned workflows cannot add assertions after the contract locks.

        The view must go through the centralized mutation service so model
        immutability is enforced even though the form itself is valid.
        """
        workflow = WorkflowFactory(is_locked=True)
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
            },
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(step.ruleset.assertions.count(), 0)
        self.assertIn("Cannot add a new assertion", response.content.decode())

    def test_create_assertion_allowed_on_locked_mutable_workflow(self):
        """Mutable history permits assertion edits even after a lock marker.

        This pins the history-policy distinction: mutable workflows are
        records of outcomes, not immutable reproducibility evidence.
        """
        workflow = WorkflowFactory(
            is_locked=True,
            history_policy=WorkflowHistoryPolicy.MUTABLE,
        )
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
            },
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(step.ruleset.assertions.count(), 1)

    def test_custom_validator_assertion_modal_renders(self):
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        custom_validator = CustomValidatorFactory(org=workflow.org)
        StepIODefinitionFactory(
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

    def test_assertion_create_modal_uses_full_width_cel_expression_field(self):
        """The create dialog should leave room for real CEL expressions.

        Long assertions become hard to read when JavaScript or crispy layout
        leaves the textarea inside the narrow assertion-type column.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_energyplus_step(workflow)
        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )

        response = self.client.get(url, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assert_cel_expression_field_uses_full_width_modal_layout(
            response.content.decode(),
        )

    def test_assertion_edit_modal_uses_full_width_cel_expression_field(self):
        """The edit dialog should preserve full-width CEL editing too.

        Create and edit share the modal shell, but the edit endpoint hydrates
        initial values separately, so both responses need to keep the textarea
        out of narrow grid columns.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_energyplus_step(workflow)
        assertion = RulesetAssertionFactory(
            ruleset=step.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_data_path="s.floor_area > 0",
            rhs={"expr": "s.floor_area > 0"},
            order=10,
        )
        url = reverse(
            "workflows:workflow_step_assertion_update",
            kwargs={
                "pk": workflow.pk,
                "step_id": step.pk,
                "assertion_id": assertion.pk,
            },
        )

        response = self.client.get(url, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assert_cel_expression_field_uses_full_width_modal_layout(
            response.content.decode(),
        )

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
        self.assertIn("Add Assertion", body)
        self.assertNotIn("Edit Assertion", body)
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

    def test_shacl_assertion_edit_modal_uses_edit_title_and_values(self):
        """The edit endpoint should not be confused with the add endpoint.

        Stale modal content is easy to miss in manual testing because the same
        Bootstrap modal hosts both forms. The server response for edit must be
        an editable form with the saved assertion values and edit labels.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_shacl_step(workflow)
        assertion = RulesetAssertionFactory(
            ruleset=step.ruleset,
            assertion_type=AssertionType.SHACL,
            operator=AssertionOperator.SPARQL_ASK,
            rhs={
                "target_graph": "data",
                "query": "ASK { ?s ?p ?o }",
                "description": "Has triples",
            },
            order=10,
        )
        url = reverse(
            "workflows:workflow_step_assertion_update",
            kwargs={
                "pk": workflow.pk,
                "step_id": step.pk,
                "assertion_id": assertion.pk,
            },
        )

        response = self.client.get(url, headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Edit Assertion", body)
        self.assertIn("Save changes", body)
        self.assertNotIn("Add Assertion", body)
        self.assertIn("Has triples", body)
        self.assertIn("ASK { ?s ?p ?o }", body)

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
        self.assertLess(
            body.index('name="shacl_description"'),
            body.index('name="severity"'),
        )

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
        self.assertIn("Outputs", body)
        self.assertNotIn("Inputs and Outputs", body)

    def test_shacl_assertion_card_uses_non_redundant_fallback_copy(self):
        """Unnamed SHACL assertions should summarize as one compact card line."""
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
                "description": "",
            },
        )

        url = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertRegex(body, r"SPARQL ASK\s*:\s*\(no description\)")
        self.assertNotIn("Against submitted RDF data graph", body)
        self.assertNotIn("SHACL SPARQL ASK", body)
        self.assertNotIn("SPARQL ASK against data graph", body)

    def test_shacl_step_renders_line_connectors_and_terminal_add_button(self):
        """The step editor should match the workflow builder flow affordance.

        Existing cards should be connected with dotted lines only. The visible
        add button belongs at the terminal position below the final item.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step = self._make_shacl_step(workflow)
        RulesetAssertionFactory(
            ruleset=step.ruleset,
            assertion_type=AssertionType.SHACL,
            operator=AssertionOperator.SPARQL_ASK,
            rhs={
                "target_graph": "data",
                "query": "ASK { ?s ?p ?o }",
                "description": "Has triples",
            },
            order=10,
        )
        url = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )

        response = self.client.get(url)

        html = response.content.decode()
        self.assertContains(response, "SHACL Validation")
        self.assertEqual(html.count('class="assertion-add-connector'), 2)
        self.assertEqual(html.count("assertion-add-connector--line-only"), 1)
        self.assertEqual(html.count("assertion-add-connector--terminal"), 1)
        self.assertEqual(html.count("assertion-add-button"), 1)
        self.assertNotIn("insert_at_start=1", html)
        self.assertNotIn("insert_after_assertion=", html)

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
        input_sig = StepIODefinitionFactory(
            validator=step.validator,
            contract_key="input-signal",
            direction="input",
        )
        output_sig = StepIODefinitionFactory(
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

    def test_schema_validator_supports_assertions(self):
        """JSON Schema validator should allow assertions after schema checks."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(
            validation_type=ValidationType.JSON_SCHEMA,
            supports_assertions=True,
        )
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        url = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("JSON Schema Validation", html)
        self.assertIn("Add assertion", html)

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
        StepIODefinitionFactory(
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


# ──────────────────────────────────────────────────────────────────────
# Diagnostic tests: which cause produces the "extra page content
# inside the assertion modal" bug?
#
# The reported symptom: opening the SHACL "Add Assertion" modal shows
# the form fields PLUS unexpected text like "Step 1: Check data
# against 223P" (the step-detail page H1) above the first field.
#
# Three competing hypotheses, deliberately tested in isolation:
#
#   Cause 1 - GET returns the full page. The assertion-create view
#             responds with workflow_step_detail.html (which contains
#             the step H1) instead of assertion_form.html. Test:
#             GET with HX-Request and look for page-shell markers.
#
#   Cause 2 - Stale DOM state between modal opens. Pure JS/DOM bug;
#             not testable in pytest. If both other tests pass, this
#             is the surviving suspect.
#
#   Cause 3 - POST error path returns the full page. The form's POST
#             handler returns workflow_step_detail.html on validation
#             failure instead of re-rendering the partial. Test: POST
#             invalid data and look for page-shell markers.
#
# Page-shell markers we look for:
#   - "<html" or "<!DOCTYPE" — definitive full-document evidence
#   - "Step Assertions" — the assertions card heading from the step
#     editor page; only present if the full page leaks in
#   - "id=\"workflowAssertionModal\"" — the modal wrapper from the
#     step detail page; the partial NEVER renders this. Its presence
#     means a full page is being returned.
#
# The partial (assertion_form.html) renders only:
#   - <form>, modal-header, modal-body, modal-footer
#   - <h5 class="modal-title">Add Assertion</h5>
#   - the crispy-rendered fields
# Nothing else. If the response contains anything else above, we have
# our culprit.
# ──────────────────────────────────────────────────────────────────────


PAGE_SHELL_MARKERS = (
    "<!doctype",
    "<html",
    'id="workflowAssertionModal"',
    "Step Assertions",
)


def _looks_like_full_page(body: str) -> tuple[bool, str]:
    """Return ``(True, marker)`` if body contains any page-shell marker.

    Case-insensitive match so ``<!doctype`` and ``<!DOCTYPE`` both hit.
    """
    haystack = body.lower()
    for marker in PAGE_SHELL_MARKERS:
        if marker.lower() in haystack:
            return True, marker
    return False, ""


class AssertionModalLeakDiagnosticTests(TestCase):
    """Pin down which cause produces the modal-content leak bug.

    Each test names ONE hypothesis. The hypothesis is "the response
    payload contains markup from the surrounding page, not just the
    form partial." A passing test = the bug is NOT in that hypothesis;
    a failing test = the bug IS in that hypothesis.
    """

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def _build_shacl_step(self):
        """Mirror _make_shacl_step on the main test class."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(
            validation_type=ValidationType.SHACL,
            supports_assertions=True,
        )
        StepIODefinitionFactory(
            validator=validator,
            contract_key="shacl_violation_count",
            label="SHACL Violation Count",
            direction="output",
        )
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        if not step.ruleset_id:
            ruleset = RulesetFactory(org=workflow.org)
            step.ruleset = ruleset
            step.save(update_fields=["ruleset"])
        return workflow, step

    def test_cause_1_get_returns_partial_not_full_page(self):
        """GET /assertions/create/ should return the form partial only.

        If this fails, the assertion-create view is returning
        workflow_step_detail.html (or a template that includes it).
        That's the most plausible single cause of "Step 1: …" text
        appearing above the form in the modal.
        """
        workflow, step = self._build_shacl_step()
        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()

        is_full_page, marker = _looks_like_full_page(body)
        self.assertFalse(
            is_full_page,
            (
                f"GET assertion-create returned a full-page response "
                f"(contains '{marker}'). Cause 1 is real: the view is "
                "rendering workflow_step_detail.html instead of "
                "assertion_form.html.\n\n"
                f"First 600 chars of response:\n{body[:600]}"
            ),
        )

    def test_cause_3_post_error_path_returns_partial_not_full_page(self):
        """POST with invalid data should re-render the partial only.

        If this fails, the assertion-create POST handler returns a full
        page on validation error. The user sees the leak only when a
        save attempt fails — but the symptom (page chrome above the
        form) is identical to cause 1.

        Trigger: POST with missing required fields. Severity is
        always required; omit it so the form is invalid.
        """
        workflow, step = self._build_shacl_step()
        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            url,
            data={
                # SHACL assertion type, but missing severity + query.
                "assertion_type": "shacl",
                "shacl_target_graph": "data",
                "shacl_query": "",  # required, empty → form invalid
            },
            headers={"hx-request": "true"},
        )
        # 200 (re-rendered form with errors) is the expected response.
        # A 302 redirect would also indicate the partial path is broken.
        self.assertEqual(
            response.status_code,
            200,
            f"Expected 200 (form re-render with errors); got "
            f"{response.status_code}. Headers: {dict(response.headers)}",
        )
        body = response.content.decode()

        is_full_page, marker = _looks_like_full_page(body)
        self.assertFalse(
            is_full_page,
            (
                f"POST assertion-create error path returned a full-page "
                f"response (contains '{marker}'). Cause 3 is real: the "
                "view's POST handler renders the full page template on "
                "validation error.\n\n"
                f"First 600 chars of response:\n{body[:600]}"
            ),
        )

    def test_get_response_contains_only_form_and_modal_chrome(self):
        """Positive shape assertion: response is bounded to the partial.

        Documents what the GET response should look like: a form
        wrapped in modal-header / modal-body / modal-footer, with the
        crispy form fields inside. Catches the case where the response
        is genuinely the partial but has been padded with unrelated
        content (e.g. someone adds a global include to the partial).
        """
        workflow, step = self._build_shacl_step()
        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.get(url, headers={"hx-request": "true"})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()

        # The partial must contain its own chrome:
        self.assertIn(
            "<form",
            body,
            "Response missing <form> — the partial may not be rendering at all.",
        )
        self.assertIn(
            'class="modal-header"',
            body,
            "Response missing modal-header chrome.",
        )
        # And must NOT contain markers of the step detail page.
        # These two are the specific markers from the symptom:
        # the step page's "Step Assertions" heading and the page H1.
        for forbidden in ("Step Assertions",):
            self.assertNotIn(
                forbidden,
                body,
                (
                    f"Response unexpectedly contains '{forbidden}' — that "
                    "text only appears on the step-detail page, not in "
                    "the assertion form partial."
                ),
            )


# ──────────────────────────────────────────────────────────────────────
# Diagnostic tests: which step of the SHACL save flow stalls?
#
# The reported symptom: after submitting a SHACL assertion, the modal
# shows the spinner placeholder indefinitely and the page underneath
# doesn't refresh to show the new assertion.
#
# The expected flow on the server side is:
#   1. POST /assertions/create/ returns 204 No Content.
#   2. The response carries an HX-Trigger header that JSON-encodes:
#        - "close-modal": "workflowAssertionModal"
#        - "assertions-changed": {"focus_assertion_id": <pk>}
#        - "steps-changed": true
#        - "toast": {...}
#   3. The client JS listens for close-modal → hides the modal.
#   4. #assertions-editor-content has hx-trigger="assertions-changed
#      from:body" → fires hx-get to refresh the assertions panel.
#
# These tests pin step 1-2 (the server side). If they pass, the
# server is sending the right signals and the failure is in the
# client-side glue (steps 3-4) — most likely a Bootstrap modal-state
# issue after the spinner-reset wipes the modal-content div.
# ──────────────────────────────────────────────────────────────────────


class ShaclAssertionSaveFlowDiagnosticTests(TestCase):
    """Pin down whether the SHACL save flow's server response is correct."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def _build_shacl_step(self):
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(
            validation_type=ValidationType.SHACL,
            supports_assertions=True,
        )
        StepIODefinitionFactory(
            validator=validator,
            contract_key="shacl_violation_count",
            label="SHACL Violation Count",
            direction="output",
        )
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        if not step.ruleset_id:
            ruleset = RulesetFactory(org=workflow.org)
            step.ruleset = ruleset
            step.save(update_fields=["ruleset"])
        return workflow, step

    def test_shacl_post_returns_204_with_hx_trigger_payload(self):
        """Verify the server returns the expected save-flow signal.

        If this fails: the server isn't telling the client to close the
        modal and/or refresh the assertions panel. The client is doing
        its job; the server isn't holding up its end.

        If this passes: the server is correct and the symptom is a
        client-side bug — most likely the modal-content reset wiping
        Bootstrap's internal modal references, or the assertions-editor
        HTMx refresh trigger not firing.
        """
        import json

        workflow, step = self._build_shacl_step()
        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            url,
            data={
                "assertion_type": "shacl",
                "shacl_description": "Test SPARQL ASK",
                "shacl_target_graph": "data",
                "shacl_query": "ASK { ?s ?p ?o }",
                "severity": "ERROR",
            },
            headers={"hx-request": "true"},
        )

        self.assertEqual(
            response.status_code,
            204,
            f"Expected 204 No Content on successful save. Got "
            f"{response.status_code}. Body: {response.content!r}",
        )
        self.assertIn(
            "HX-Trigger",
            response.headers,
            "Server didn't return an HX-Trigger header — the client "
            "has nothing to listen for. Modal can't close, panel can't "
            "refresh.",
        )

        payload = json.loads(response.headers["HX-Trigger"])

        # The client JS listens for "close-modal" with the modal id.
        # Without this, the modal stays open showing the spinner.
        self.assertIn(
            "close-modal",
            payload,
            "HX-Trigger payload missing 'close-modal' key. Modal won't "
            f"close after save. Payload: {payload!r}",
        )
        self.assertEqual(
            payload["close-modal"],
            "workflowAssertionModal",
            f"close-modal value should name the assertion modal. Got: "
            f"{payload['close-modal']!r}",
        )

        # The #assertions-editor-content div has hx-trigger=
        # "assertions-changed from:body". Without this event, the
        # panel never re-fetches and the new assertion doesn't appear.
        self.assertIn(
            "assertions-changed",
            payload,
            "HX-Trigger payload missing 'assertions-changed' key. The "
            f"assertions panel won't refresh. Payload: {payload!r}",
        )

        # The new assertion's PK is needed for the post-refresh scroll
        # + highlight animation.
        detail = payload["assertions-changed"]
        self.assertIsInstance(
            detail,
            dict,
            f"assertions-changed should be a dict with focus_assertion_id. "
            f"Got: {detail!r}",
        )
        self.assertIn(
            "focus_assertion_id",
            detail,
            f"assertions-changed missing focus_assertion_id. Got: {detail!r}",
        )

    def test_shacl_post_uses_partial_refresh_not_hx_refresh(self):
        """SHACL saves should use the same partial-refresh flow as other rows.

        The modal reset bug was client-side: resetting the modal contents for
        POST requests corrupted the form interaction. Once reset is scoped to
        GET/open requests, SHACL can keep the normal close-modal +
        assertions-changed behavior and should not force a full page reload.
        """
        workflow, step = self._build_shacl_step()
        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            url,
            data={
                "assertion_type": "shacl",
                "shacl_description": "Test",
                "shacl_target_graph": "data",
                "shacl_query": "ASK { ?s ?p ?o }",
                "severity": "ERROR",
            },
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 204)
        self.assertNotIn(
            "HX-Refresh",
            response.headers,
            "SHACL saves should not force a full page reload now that the "
            "modal reset only runs for GET/open requests. Got headers: "
            f"{dict(response.headers)}",
        )

    def test_short_type_label_maps_to_assertion_mechanism(self):
        """The short_type_label property names the assertion mechanism.

        On a SHACL step, the assertion card pill is more useful when it
        says what KIND of assertion this is (SPARQL / CEL / Basic) than
        when it says what validator family it belongs to (which is
        "SHACL" for every assertion on a SHACL step — redundant).
        """
        from validibot.validations.models import RulesetAssertion

        cases = [
            (AssertionType.SHACL, "SPARQL"),
            (AssertionType.CEL_EXPRESSION, "CEL"),
            (AssertionType.BASIC, "Basic"),
        ]
        for assertion_type, expected_label in cases:
            assertion = RulesetAssertion(assertion_type=assertion_type)
            self.assertEqual(
                str(assertion.short_type_label),
                expected_label,
                f"{assertion_type} should label as {expected_label}, "
                f"got {assertion.short_type_label!r}",
            )

    def test_non_shacl_post_does_not_send_hx_refresh(self):
        """Non-SHACL saves rely on the partial-refresh flow, not HX-Refresh.

        Adding HX-Refresh to assertion saves would lose the
        scroll-and-highlight animation that the partial refresh provides.
        This guards against reintroducing the old full-refresh workaround.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
        StepIODefinitionFactory(
            validator=validator,
            contract_key="facility_electric_demand_w",
            direction="output",
        )
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        if not step.ruleset_id:
            ruleset = RulesetFactory(org=workflow.org)
            step.ruleset = ruleset
            step.save(update_fields=["ruleset"])
        url = reverse(
            "workflows:workflow_step_assertion_create",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )
        response = self.client.post(
            url,
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
        self.assertNotIn(
            "HX-Refresh",
            response.headers,
            "Non-SHACL saves should NOT set HX-Refresh — they rely on "
            "the partial-refresh flow for the scroll-and-highlight UX. "
            f"Got headers: {dict(response.headers)}",
        )
