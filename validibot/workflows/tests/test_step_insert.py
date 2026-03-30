"""Tests for inserting workflow steps between existing steps.

This test suite covers the inline "insert step here" feature that lets
users add a new step at a specific position in the workflow, rather than
only appending at the end.  The feature threads an ``insert_after_step``
parameter through the wizard → create redirect chain and uses it to set
the new step's order value so that resequencing places it correctly.

Coverage includes:

- Wizard view: ``insert_after_step`` flows from GET → template → POST → redirect
- Step ordering: new steps land at the correct position after insertion
- Edge cases: insert after first step, last step, single-step workflows
- Resequencing: order values normalise to clean multiples of 10 after insert
- Guard rails: invalid or missing ``insert_after_step`` falls back to append
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import IntegrationActionType
from validibot.actions.models import ActionDefinition
from validibot.users.constants import RoleCode
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.constants import ValidationType
from validibot.validations.models import Validator
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def seed_roles(db):
    ensure_all_roles_exist()


def _ensure_validator(
    validation_type: str = ValidationType.BASIC,
    slug: str = "basic",
    name: str = "Basic Validator",
) -> Validator:
    validator, _ = Validator.objects.get_or_create(
        validation_type=validation_type,
        slug=slug,
        defaults={"name": name, "description": name},
    )
    return validator


def _ensure_action_definition() -> ActionDefinition:
    definition, _ = ActionDefinition.objects.get_or_create(
        action_category=ActionCategoryType.INTEGRATION,
        type=IntegrationActionType.SLACK_MESSAGE,
        defaults={
            "slug": "integration-slack-message",
            "name": "Slack message",
            "description": "Test action",
            "icon": "bi-slack",
        },
    )
    return definition


def _login_for_workflow(client, workflow):
    """Log in as the workflow owner with Author role."""
    user = workflow.user
    membership = user.memberships.get(org=workflow.org)
    membership.set_roles({RoleCode.AUTHOR})
    user.set_current_org(workflow.org)
    user.refresh_from_db()
    client.force_login(user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()


def _create_workflow_with_steps(n: int = 3) -> tuple:
    """Create a workflow with *n* ordered Basic validator steps.

    Returns (workflow, [step1, step2, ...]) with steps ordered 10, 20, 30...
    """
    validator = _ensure_validator()
    workflow = WorkflowFactory()
    steps = []
    for i in range(n):
        step = WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            name=f"Step {i + 1}",
            order=(i + 1) * 10,
        )
        steps.append(step)
    return workflow, steps


def _get_step_names_ordered(workflow) -> list[str]:
    """Return step names in their current order."""
    return list(
        workflow.steps.order_by("order", "pk").values_list("name", flat=True),
    )


def _get_step_orders(workflow) -> list[int]:
    """Return step order values in sorted order."""
    return list(
        workflow.steps.order_by("order", "pk").values_list("order", flat=True),
    )


# ---------------------------------------------------------------------------
# Wizard view: insert_after_step parameter threading
# ---------------------------------------------------------------------------


class TestWizardInsertAfterStep:
    """The wizard view must carry insert_after_step through the full flow."""

    def test_wizard_get_passes_insert_after_step_to_template(self, client):
        """GET with ?insert_after_step=N should include a hidden field in
        the rendered form so the POST carries the value forward."""
        workflow, steps = _create_workflow_with_steps(2)
        _login_for_workflow(client, workflow)

        url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
        response = client.get(
            f"{url}?insert_after_step={steps[0].pk}",
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == HTTPStatus.OK
        html = response.content.decode()
        assert 'name="insert_after_step"' in html
        assert f'value="{steps[0].pk}"' in html

    def test_wizard_get_without_insert_after_omits_hidden_field(self, client):
        """GET without insert_after_step should not include the hidden field."""
        workflow = WorkflowFactory()
        _login_for_workflow(client, workflow)

        url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
        response = client.get(url, HTTP_HX_REQUEST="true")

        assert response.status_code == HTTPStatus.OK
        html = response.content.decode()
        assert 'name="insert_after_step"' not in html

    def test_wizard_post_forwards_insert_after_step_to_redirect(self, client):
        """POST with insert_after_step should append it as a query param
        on the HX-Redirect URL to the create view."""
        workflow, steps = _create_workflow_with_steps(2)
        _login_for_workflow(client, workflow)
        validator = _ensure_validator()

        url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
        response = client.post(
            url,
            data={
                "stage": "select",
                "choice": f"validator:{validator.pk}",
                "insert_after_step": str(steps[0].pk),
            },
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == HTTPStatus.NO_CONTENT
        redirect_url = response.headers.get("HX-Redirect", "")
        assert f"insert_after_step={steps[0].pk}" in redirect_url

    def test_wizard_post_without_insert_after_has_no_query_param(self, client):
        """POST without insert_after_step should not add query param."""
        workflow = WorkflowFactory()
        _login_for_workflow(client, workflow)
        validator = _ensure_validator()

        url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
        response = client.post(
            url,
            data={
                "stage": "select",
                "choice": f"validator:{validator.pk}",
            },
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == HTTPStatus.NO_CONTENT
        redirect_url = response.headers.get("HX-Redirect", "")
        assert "insert_after_step" not in redirect_url

    def test_wizard_post_forwards_insert_after_for_action_steps(self, client):
        """insert_after_step should also flow through for action step
        selections, not just validator steps."""
        workflow, steps = _create_workflow_with_steps(2)
        _login_for_workflow(client, workflow)
        definition = _ensure_action_definition()

        url = reverse("workflows:workflow_step_wizard", args=[workflow.pk])
        response = client.post(
            url,
            data={
                "stage": "select",
                "choice": f"action:{definition.pk}",
                "insert_after_step": str(steps[0].pk),
            },
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == HTTPStatus.NO_CONTENT
        redirect_url = response.headers.get("HX-Redirect", "")
        assert f"insert_after_step={steps[0].pk}" in redirect_url


# ---------------------------------------------------------------------------
# Step creation with insert_after_step
# ---------------------------------------------------------------------------


class TestStepInsertOrdering:
    """Creating a step with insert_after_step should place it at the
    correct position and resequence all steps to clean multiples of 10."""

    def test_insert_between_first_and_second_step(self, client):
        """Inserting after step 1 in a 3-step workflow should place the
        new step at position 2, pushing the others down."""
        workflow, steps = _create_workflow_with_steps(3)
        _login_for_workflow(client, workflow)
        validator = _ensure_validator()

        create_url = reverse(
            "workflows:workflow_step_create",
            args=[workflow.pk, validator.pk],
        )
        response = client.post(
            f"{create_url}?insert_after_step={steps[0].pk}",
            data={"name": "Inserted Step"},
        )

        assert response.status_code == HTTPStatus.FOUND
        names = _get_step_names_ordered(workflow)
        assert names == ["Step 1", "Inserted Step", "Step 2", "Step 3"]

    def test_insert_between_second_and_third_step(self, client):
        """Inserting after step 2 in a 3-step workflow should place the
        new step at position 3."""
        workflow, steps = _create_workflow_with_steps(3)
        _login_for_workflow(client, workflow)
        validator = _ensure_validator()

        create_url = reverse(
            "workflows:workflow_step_create",
            args=[workflow.pk, validator.pk],
        )
        response = client.post(
            f"{create_url}?insert_after_step={steps[1].pk}",
            data={"name": "Inserted Step"},
        )

        assert response.status_code == HTTPStatus.FOUND
        names = _get_step_names_ordered(workflow)
        assert names == ["Step 1", "Step 2", "Inserted Step", "Step 3"]

    def test_insert_after_last_step_appends(self, client):
        """Inserting after the last step should append at the end, same
        as the default append behaviour."""
        workflow, steps = _create_workflow_with_steps(3)
        _login_for_workflow(client, workflow)
        validator = _ensure_validator()

        create_url = reverse(
            "workflows:workflow_step_create",
            args=[workflow.pk, validator.pk],
        )
        response = client.post(
            f"{create_url}?insert_after_step={steps[2].pk}",
            data={"name": "Inserted Step"},
        )

        assert response.status_code == HTTPStatus.FOUND
        names = _get_step_names_ordered(workflow)
        assert names == ["Step 1", "Step 2", "Step 3", "Inserted Step"]

    def test_insert_in_single_step_workflow(self, client):
        """Inserting after the only step should create a two-step workflow
        with the new step second."""
        workflow, steps = _create_workflow_with_steps(1)
        _login_for_workflow(client, workflow)
        validator = _ensure_validator()

        create_url = reverse(
            "workflows:workflow_step_create",
            args=[workflow.pk, validator.pk],
        )
        response = client.post(
            f"{create_url}?insert_after_step={steps[0].pk}",
            data={"name": "Second Step"},
        )

        assert response.status_code == HTTPStatus.FOUND
        names = _get_step_names_ordered(workflow)
        assert names == ["Step 1", "Second Step"]

    def test_orders_are_resequenced_after_insert(self, client):
        """After insertion, order values should be normalised to clean
        multiples of 10 (10, 20, 30, 40)."""
        workflow, steps = _create_workflow_with_steps(3)
        _login_for_workflow(client, workflow)
        validator = _ensure_validator()

        create_url = reverse(
            "workflows:workflow_step_create",
            args=[workflow.pk, validator.pk],
        )
        client.post(
            f"{create_url}?insert_after_step={steps[0].pk}",
            data={"name": "Inserted Step"},
        )

        orders = _get_step_orders(workflow)
        assert orders == [10, 20, 30, 40]

    def test_without_insert_after_appends_at_end(self, client):
        """Without insert_after_step the step should append at the end,
        preserving the original default behaviour."""
        workflow, steps = _create_workflow_with_steps(3)
        _login_for_workflow(client, workflow)
        validator = _ensure_validator()

        create_url = reverse(
            "workflows:workflow_step_create",
            args=[workflow.pk, validator.pk],
        )
        response = client.post(
            create_url,
            data={"name": "Appended Step"},
        )

        assert response.status_code == HTTPStatus.FOUND
        names = _get_step_names_ordered(workflow)
        assert names == ["Step 1", "Step 2", "Step 3", "Appended Step"]


# ---------------------------------------------------------------------------
# Edge cases and guard rails
# ---------------------------------------------------------------------------


class TestStepInsertEdgeCases:
    """Edge cases: invalid insert_after_step values, non-existent step IDs,
    and concurrent ordering scenarios."""

    def test_invalid_insert_after_step_falls_back_to_append(self, client):
        """A non-integer insert_after_step should be silently ignored and
        the step appended at the end."""
        workflow, steps = _create_workflow_with_steps(2)
        _login_for_workflow(client, workflow)
        validator = _ensure_validator()

        create_url = reverse(
            "workflows:workflow_step_create",
            args=[workflow.pk, validator.pk],
        )
        response = client.post(
            f"{create_url}?insert_after_step=not-a-number",
            data={"name": "Appended Step"},
        )

        assert response.status_code == HTTPStatus.FOUND
        names = _get_step_names_ordered(workflow)
        assert names[-1] == "Appended Step"

    def test_nonexistent_step_id_falls_back_to_append(self, client):
        """An insert_after_step pointing to a step ID that does not exist
        in this workflow should fall back to appending."""
        workflow, steps = _create_workflow_with_steps(2)
        _login_for_workflow(client, workflow)
        validator = _ensure_validator()

        create_url = reverse(
            "workflows:workflow_step_create",
            args=[workflow.pk, validator.pk],
        )
        response = client.post(
            f"{create_url}?insert_after_step=99999",
            data={"name": "Appended Step"},
        )

        assert response.status_code == HTTPStatus.FOUND
        names = _get_step_names_ordered(workflow)
        assert names[-1] == "Appended Step"

    def test_multiple_sequential_inserts_maintain_correct_order(self, client):
        """Inserting two steps sequentially at the same position should
        result in the correct final ordering."""
        workflow, steps = _create_workflow_with_steps(2)
        _login_for_workflow(client, workflow)
        validator = _ensure_validator()

        create_url = reverse(
            "workflows:workflow_step_create",
            args=[workflow.pk, validator.pk],
        )

        # Insert "A" after step 1
        client.post(
            f"{create_url}?insert_after_step={steps[0].pk}",
            data={"name": "Insert A"},
        )
        # Insert "B" also after step 1 (should go between step 1 and A)
        client.post(
            f"{create_url}?insert_after_step={steps[0].pk}",
            data={"name": "Insert B"},
        )

        names = _get_step_names_ordered(workflow)
        assert names == ["Step 1", "Insert B", "Insert A", "Step 2"]

    def test_insert_does_not_violate_unique_constraint(self, client):
        """The insert operation must not trigger a unique constraint
        violation on (workflow_id, order), even with many existing steps."""
        workflow, steps = _create_workflow_with_steps(5)
        _login_for_workflow(client, workflow)
        validator = _ensure_validator()

        create_url = reverse(
            "workflows:workflow_step_create",
            args=[workflow.pk, validator.pk],
        )
        # Insert between each pair of existing steps
        for step in steps[:-1]:
            response = client.post(
                f"{create_url}?insert_after_step={step.pk}",
                data={"name": f"After {step.name}"},
            )
            assert response.status_code == HTTPStatus.FOUND, (
                f"Insert after {step.name} should not raise IntegrityError"
            )

        # 5 original + 4 inserted = 9 total steps
        total = workflow.steps.count()
        assert total == 9  # noqa: PLR2004
        orders = _get_step_orders(workflow)
        assert orders == list(range(10, 100, 10))


# ---------------------------------------------------------------------------
# _compute_insert_order unit tests
# ---------------------------------------------------------------------------


class TestComputeInsertOrder:
    """Direct tests for the _compute_insert_order helper to verify
    ordering logic independent of the view layer."""

    def test_insert_after_first_of_three(self):
        """Order should land between step 1 (order 10) and step 2 (order 20)."""
        from validibot.workflows.views_helpers import _compute_insert_order

        workflow, steps = _create_workflow_with_steps(3)
        order = _compute_insert_order(workflow, steps[0].pk)

        assert 10 < order < 20, (  # noqa: PLR2004
            f"Expected order between 10 and 20, got {order}"
        )

    def test_insert_after_second_of_three(self):
        """Order should land between step 2 (order 20) and step 3 (order 30)."""
        from validibot.workflows.views_helpers import _compute_insert_order

        workflow, steps = _create_workflow_with_steps(3)
        order = _compute_insert_order(workflow, steps[1].pk)

        assert 20 < order < 30, (  # noqa: PLR2004
            f"Expected order between 20 and 30, got {order}"
        )

    def test_insert_after_last_step(self):
        """Order should be greater than the last step's order."""
        from validibot.workflows.views_helpers import _compute_insert_order

        workflow, steps = _create_workflow_with_steps(3)
        order = _compute_insert_order(workflow, steps[2].pk)

        assert order > 30, f"Expected order > 30, got {order}"  # noqa: PLR2004

    def test_insert_after_none_appends(self):
        """Passing None should append at the end."""
        from validibot.workflows.views_helpers import _compute_insert_order

        workflow, steps = _create_workflow_with_steps(3)
        order = _compute_insert_order(workflow, None)

        assert order > 30, f"Expected order > 30, got {order}"  # noqa: PLR2004

    def test_insert_after_nonexistent_step_appends(self):
        """A step PK that does not belong to the workflow should fall back
        to the append-at-end behaviour."""
        from validibot.workflows.views_helpers import _compute_insert_order

        workflow, steps = _create_workflow_with_steps(2)
        order = _compute_insert_order(workflow, 99999)

        assert order > 20, f"Expected order > 20, got {order}"  # noqa: PLR2004

    def test_insert_after_resequences_first(self):
        """If steps have non-standard order values, resequencing should
        normalise them before computing the insertion point."""
        from validibot.workflows.views_helpers import _compute_insert_order

        validator = _ensure_validator()
        workflow = WorkflowFactory()
        # Create steps with irregular gaps
        s1 = WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            name="A",
            order=5,
        )
        WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            name="B",
            order=7,
        )
        WorkflowStepFactory(
            workflow=workflow,
            validator=validator,
            name="C",
            order=8,
        )

        order = _compute_insert_order(workflow, s1.pk)

        # After resequencing: A=10, B=20, C=30.  Insert after A → 11
        s1.refresh_from_db()
        assert s1.order == 10, "resequence should have set A's order to 10"  # noqa: PLR2004
        assert order == 11  # noqa: PLR2004

    def test_empty_workflow_append(self):
        """Appending to an empty workflow should produce order 10."""
        from validibot.workflows.views_helpers import _compute_insert_order

        workflow = WorkflowFactory()
        order = _compute_insert_order(workflow, None)

        assert order == 10  # noqa: PLR2004


# ---------------------------------------------------------------------------
# Template rendering: inline insert buttons
# ---------------------------------------------------------------------------


class TestStepListInlineButtons:
    """The step list partial should render inline insert buttons between
    steps when the user has management permissions."""

    def test_inline_buttons_rendered_between_steps(self, client):
        """Each connector between steps should contain an inline insert
        button with the correct insert_after_step param."""
        workflow, steps = _create_workflow_with_steps(3)
        _login_for_workflow(client, workflow)

        url = reverse("workflows:workflow_step_list", args=[workflow.pk])
        response = client.get(url, HTTP_HX_REQUEST="true")

        assert response.status_code == HTTPStatus.OK
        html = response.content.decode()
        # Should have 2 inline buttons (between steps 1-2 and 2-3)
        assert html.count("workflow-step-add-inline") == 2  # noqa: PLR2004
        assert f"insert_after_step={steps[0].pk}" in html
        assert f"insert_after_step={steps[1].pk}" in html
        # Step 3 is last — no inline button after it (terminal button instead)
        assert f"insert_after_step={steps[2].pk}" not in html

    def test_no_inline_buttons_for_single_step(self, client):
        """A single-step workflow has no between-step connectors, so no
        inline insert buttons should appear."""
        workflow, steps = _create_workflow_with_steps(1)
        _login_for_workflow(client, workflow)

        url = reverse("workflows:workflow_step_list", args=[workflow.pk])
        response = client.get(url, HTTP_HX_REQUEST="true")

        assert response.status_code == HTTPStatus.OK
        html = response.content.decode()
        assert "workflow-step-add-inline" not in html

    def test_terminal_add_button_still_rendered(self, client):
        """The terminal + button after the last step should still be
        present alongside the inline insert buttons."""
        workflow, _steps = _create_workflow_with_steps(2)
        _login_for_workflow(client, workflow)

        url = reverse("workflows:workflow_step_list", args=[workflow.pk])
        response = client.get(url, HTTP_HX_REQUEST="true")

        assert response.status_code == HTTPStatus.OK
        html = response.content.decode()
        assert "workflow-step-add-terminal" in html
        assert "workflow-step-add-inline" in html
