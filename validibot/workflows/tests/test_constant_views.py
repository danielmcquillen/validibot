"""Tests for the Constants editor views (the ``c.*`` namespace UI).

ADR-2026-06-18 Phase 3: authors manage Constants through a modal CRUD editor
that mirrors the signal-mapping editor. This suite verifies the HTTP surface —
permissions, the create/edit/delete/reference-block flows, and that the
type-coercion contract surfaces as a form error rather than a 500.

Why this matters: the editor is the only way an author creates a constant in
the product, so its permission gate and its "block delete of a referenced
constant" guard are user-facing correctness, not nicety.
"""

from __future__ import annotations

import json
from http import HTTPStatus

from django.test import Client
from django.test import TestCase
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.models import RulesetAssertion
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.constants import WorkflowConstantType
from validibot.workflows.models import WorkflowConstant
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


def _login_as_author(client: Client, workflow):
    """Log in as the workflow owner with AUTHOR (manage) permissions."""
    membership = workflow.user.memberships.get(org=workflow.org)
    membership.set_roles({RoleCode.AUTHOR})
    workflow.user.set_current_org(workflow.org)
    client.force_login(workflow.user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()
    return workflow.user


class TestConstantEditorPage(TestCase):
    """The Constants editor page renders for an authorized manager."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_get_returns_editor_page(self):
        """GET renders the full editor page with the workflow in context.

        This is the primary UI for managing constants, so it must use the
        right template and be reachable by the workflow's author.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse("workflows:workflow_constants", kwargs={"pk": workflow.pk})

        response = self.client.get(url)

        assert response.status_code == HTTPStatus.OK
        self.assertTemplateUsed(response, "workflows/workflow_constants.html")

    def test_htmx_request_returns_table_partial(self):
        """An HTMx request returns only the table fragment for in-place refresh.

        The page wires ``constants-changed`` to reload just the table; the view
        must honour ``HX-Request`` so the modal flows don't reload the whole page.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse("workflows:workflow_constants", kwargs={"pk": workflow.pk})

        response = self.client.get(url, headers={"hx-request": "true"})

        assert response.status_code == HTTPStatus.OK
        self.assertTemplateUsed(response, "workflows/partials/constant_table.html")

    def test_non_manager_is_forbidden(self):
        """A user without manage permission cannot view the editor.

        Constants are workflow authoring; the gate must reject non-managers
        (defense against horizontal privilege escalation across orgs).
        """
        workflow = WorkflowFactory()
        outsider = UserFactory()
        self.client.force_login(outsider)
        url = reverse("workflows:workflow_constants", kwargs={"pk": workflow.pk})

        response = self.client.get(url)

        assert response.status_code in (HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND)


class TestConstantCreate(TestCase):
    """Creating a constant via the modal form."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_create_number_constant_persists_decimal_string(self):
        """A valid NUMBER create persists the exact decimal string and triggers.

        End-to-end the form coerces ``0.40`` to the canonical decimal string and
        the success response carries the ``constants-changed`` HTMx event that
        refreshes the table.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_constant_create",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.post(
            url,
            data={
                "name": "energy_price",
                "data_type": WorkflowConstantType.NUMBER,
                "value": "0.40",
                "description": "agreed price",
            },
        )

        assert response.status_code == HTTPStatus.NO_CONTENT
        trigger = json.loads(response.headers["HX-Trigger"])
        assert "constants-changed" in trigger
        constant = WorkflowConstant.objects.get(workflow=workflow, name="energy_price")
        assert constant.value == "0.40"

    def test_invalid_value_returns_form_with_error(self):
        """A non-numeric NUMBER re-renders the form (200) with a field error.

        The type contract is enforced at the form layer, so the author sees the
        problem inline — not a 500 and not a silently mistyped constant.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        url = reverse(
            "workflows:workflow_constant_create",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.post(
            url,
            data={
                "name": "energy_price",
                "data_type": WorkflowConstantType.NUMBER,
                "value": "abc",
                "description": "",
            },
        )

        assert response.status_code == HTTPStatus.OK
        assert not WorkflowConstant.objects.filter(workflow=workflow).exists()

    def test_create_requires_manage_permission(self):
        """A non-manager cannot create a constant (POST is gated too)."""
        workflow = WorkflowFactory()
        outsider = UserFactory()
        self.client.force_login(outsider)
        url = reverse(
            "workflows:workflow_constant_create",
            kwargs={"pk": workflow.pk},
        )

        response = self.client.post(
            url,
            data={
                "name": "x",
                "data_type": WorkflowConstantType.STRING,
                "value": "y",
            },
        )

        assert response.status_code in (HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND)
        assert not WorkflowConstant.objects.filter(workflow=workflow).exists()


class TestConstantDelete(TestCase):
    """Deleting a constant, including the referenced-by-assertion block."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_delete_unreferenced_constant(self):
        """An unreferenced constant deletes and fires ``constants-changed``."""
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        constant = WorkflowConstant.objects.create(
            workflow=workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        url = reverse(
            "workflows:workflow_constant_delete",
            kwargs={"pk": workflow.pk, "constant_id": constant.pk},
        )

        response = self.client.post(url)

        assert response.status_code == HTTPStatus.NO_CONTENT
        assert not WorkflowConstant.objects.filter(pk=constant.pk).exists()

    def test_delete_blocked_when_referenced_by_assertion(self):
        """Deleting a constant referenced by a CEL assertion is blocked.

        Silently removing ``c.energy_price`` while an assertion still reads it
        would break that rule at the next run — so the delete is refused with a
        clear message and the constant survives.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(validation_type=ValidationType.BASIC)
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        # A workflow step whose ruleset holds the referencing assertion.
        workflow.steps.create(validator=validator, ruleset=ruleset, order=10)
        RulesetAssertion.objects.create(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.EQ,
            rhs={"expr": "payload.price == c.energy_price"},
        )
        constant = WorkflowConstant.objects.create(
            workflow=workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        url = reverse(
            "workflows:workflow_constant_delete",
            kwargs={"pk": workflow.pk, "constant_id": constant.pk},
        )

        response = self.client.post(url)

        # HTMx error responses use 200 + an error toast, not a 4xx.
        assert response.status_code == HTTPStatus.OK
        assert WorkflowConstant.objects.filter(pk=constant.pk).exists()

    def test_delete_blocked_when_referenced_by_basic_assertion_target(self):
        """Deleting a constant used as a Basic assertion's TARGET is blocked.

        The ADR allows a Basic assertion to reference a constant as its
        ``target_data_path`` (e.g. ``c.energy_price``), not only inside a CEL
        expression. Deleting the constant would silently break that Basic rule at
        the next run, so the guard must scan ``target_data_path`` too — the exact
        case the original CEL-only guard missed.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(validation_type=ValidationType.BASIC)
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        workflow.steps.create(validator=validator, ruleset=ruleset, order=10)
        # A Basic assertion whose TARGET (not a CEL expression) is the constant.
        RulesetAssertion.objects.create(
            ruleset=ruleset,
            assertion_type=AssertionType.BASIC,
            operator=AssertionOperator.LT,
            target_data_path="c.energy_price",
            rhs={"value": 120},
        )
        constant = WorkflowConstant.objects.create(
            workflow=workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        url = reverse(
            "workflows:workflow_constant_delete",
            kwargs={"pk": workflow.pk, "constant_id": constant.pk},
        )

        response = self.client.post(url)

        # Blocked: HTMx error response (200 + toast) and the constant survives.
        assert response.status_code == HTTPStatus.OK
        assert WorkflowConstant.objects.filter(pk=constant.pk).exists()


class TestConstantsVisibleWhereAuthored(TestCase):
    """The "always-visible constants" requirement (ADR-2026-06-18 Phase 3b).

    A constant only earns its keep if people can *see* it — with its value —
    where they need it: the author reference panel, the assertion autocomplete,
    and the public info page. These tests assert each surface actually renders
    the value (not just the name), which is the whole point of a constant over
    an inline literal.
    """

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_reference_panel_shows_constant_with_value(self):
        """The step editor "Available Data" panel lists ``c.name`` and its value.

        Unlike signals (name + path), a constant's value is design-time-known,
        so the panel shows it — the author sees exactly what they're asserting
        against without opening the Constants editor.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(validation_type=ValidationType.BASIC)
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        WorkflowConstant.objects.create(
            workflow=workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        url = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": workflow.pk, "step_id": step.pk},
        )

        response = self.client.get(url)
        body = response.content.decode()

        assert response.status_code == HTTPStatus.OK
        assert "c.energy_price" in body
        # The exact stored value (decimal precision preserved) is shown.
        assert "0.40" in body

    def test_autocomplete_includes_constant_group_with_value(self):
        """The assertion-target autocomplete includes ``c.name`` with its value.

        Exercises ``get_catalog_choices`` through the step editor: the constant
        appears as a ``c.<name>`` choice whose label shows the value and the
        "Constant" group suffix.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        validator = ValidatorFactory(validation_type=ValidationType.BASIC)
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        WorkflowConstant.objects.create(
            workflow=workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        # Directly exercise the choice builder (the autocomplete data source)
        # on the mixin that owns it — this is the single source the assertion
        # form's autocomplete draws from.
        from validibot.workflows.mixins import WorkflowStepAssertionsMixin

        mixin = WorkflowStepAssertionsMixin()
        mixin.step = step
        choices = mixin.get_catalog_choices()

        values = {value for value, _label in choices}
        assert "c.energy_price" in values
        label = next(label for value, label in choices if value == "c.energy_price")
        assert "0.40" in label
        assert "Constant" in label

    def test_public_info_page_lists_constants_with_values(self):
        """A published info page shows constants and their values to a submitter.

        A submitter must be able to see the fixed thresholds their data will be
        judged against *before* they submit — the transparency guarantee. Safe
        to publish because a constant is workflow-defined, not submission-derived.
        """
        workflow = WorkflowFactory(make_info_page_public=True)
        WorkflowConstant.objects.create(
            workflow=workflow,
            name="energy_price",
            data_type=WorkflowConstantType.NUMBER,
            value="0.40",
        )
        url = reverse(
            "workflow_public_info",
            kwargs={"workflow_uuid": workflow.uuid},
        )

        # Anonymous request — the page is published.
        response = self.client.get(url)
        body = response.content.decode()

        assert response.status_code == HTTPStatus.OK
        assert "c.energy_price" in body
        assert "0.40" in body
