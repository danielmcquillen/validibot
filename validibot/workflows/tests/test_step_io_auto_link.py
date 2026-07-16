"""Tests for the step I/O auto-link view.

The auto-link view (``WorkflowStepIOAutoLinkView``) provides a
one-click action to connect a step input to a workflow-level
signal mapping with the same name. When the user clicks the link button
on an unmapped validator input, the view:

1. Looks for a ``WorkflowSignalMapping`` whose ``name`` matches the
   input definition's ``contract_key``.
2. If found, creates or updates the ``StepInputBinding`` to set
   ``source_scope`` to SIGNAL and ``source_data_path`` to the signal name.
3. If not found, returns a warning message.

These tests verify:

* **Success** -- matching mapping exists, binding is created/updated.
* **No match** -- no matching mapping, warning message returned.
* **Existing binding** -- an existing empty binding gets updated.
* **Access control** -- manage permission is required.
* **Definition scoping** -- definitions from other steps/validators return 404.
"""

from __future__ import annotations

from django.contrib.messages import get_messages
from django.test import Client
from django.test import TestCase
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import ValidationType
from validibot.validations.models import StepInputBinding
from validibot.validations.models import StepIODefinition
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.models import WorkflowSignalMapping
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


def _login_as_author(client: Client, workflow):
    """Log in as the workflow owner with author permissions."""
    membership = workflow.user.memberships.get(org=workflow.org)
    membership.set_roles({RoleCode.AUTHOR})
    workflow.user.set_current_org(workflow.org)
    client.force_login(workflow.user)
    session = client.session
    session["active_org_id"] = workflow.org_id
    session.save()
    return workflow.user


def _create_step_with_input(workflow):
    """Create a workflow step with an input StepIODefinition.

    Returns (step, io_definition) where the definition has
    ``contract_key="panel_area"`` and direction INPUT.
    """
    validator = ValidatorFactory(
        org=workflow.org,
        validation_type=ValidationType.BASIC,
        is_system=False,
    )
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        order=10,
    )
    io_definition = StepIODefinition.objects.create(
        workflow_step=step,
        contract_key="panel_area",
        direction=StepIODirection.INPUT,
    )
    return step, io_definition


def _auto_link_url(workflow, step, io_definition):
    return reverse(
        "workflows:workflow_step_io_auto_link",
        kwargs={
            "pk": workflow.pk,
            "step_id": step.pk,
            "io_definition_id": io_definition.pk,
        },
    )


# ── Success cases ─────────────────────────────────────────────────────
# When a WorkflowSignalMapping with the same name as the input definition's
# contract_key exists, auto-link should wire them together.


class TestAutoLinkSuccess(TestCase):
    """Auto-link creates or updates the binding when a match is found."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_auto_link_creates_binding(self):
        """When no binding exists yet, auto-link should create one with
        SIGNAL scope and the signal name as the source path.

        This is the primary use case: an FMU upload creates input definitions with
        no bindings, and the author clicks the link button to wire them
        to matching workflow-level signals.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step, io_definition = _create_step_with_input(workflow)
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="panel_area",
            source_path="building.panel_area",
        )

        url = _auto_link_url(workflow, step, io_definition)
        response = self.client.post(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Refresh"], "true")

        binding = StepInputBinding.objects.get(
            workflow_step=step,
            io_definition=io_definition,
        )
        self.assertEqual(binding.source_data_path, "panel_area")
        self.assertEqual(binding.source_scope, BindingSourceScope.SIGNAL)

        msgs = list(get_messages(response.wsgi_request))
        self.assertEqual(len(msgs), 1)
        self.assertIn("panel_area", str(msgs[0]))

    def test_auto_link_updates_existing_empty_binding(self):
        """When a binding already exists with an empty source_data_path,
        auto-link should update it rather than creating a duplicate.

        This happens when ``ensure_step_input_bindings()`` pre-creates
        bindings with empty paths after FMU upload.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step, io_definition = _create_step_with_input(workflow)
        StepInputBinding.objects.create(
            workflow_step=step,
            io_definition=io_definition,
            source_scope=BindingSourceScope.SUBMISSION_PAYLOAD,
            source_data_path="",
            is_required=True,
        )
        WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name="panel_area",
            source_path="building.panel_area",
        )

        url = _auto_link_url(workflow, step, io_definition)
        response = self.client.post(url)

        self.assertEqual(response.status_code, 200)
        binding = StepInputBinding.objects.get(
            workflow_step=step,
            io_definition=io_definition,
        )
        self.assertEqual(binding.source_data_path, "panel_area")
        self.assertEqual(binding.source_scope, BindingSourceScope.SIGNAL)
        # Only one binding should exist (no duplicate).
        self.assertEqual(
            StepInputBinding.objects.filter(
                workflow_step=step,
                io_definition=io_definition,
            ).count(),
            1,
        )


# ── No match ──────────────────────────────────────────────────────────
# When no WorkflowSignalMapping exists with a matching name, the view
# should return a warning message guiding the author to create one.


class TestAutoLinkNoMatch(TestCase):
    """Auto-link shows a warning when no matching workflow signal exists."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_no_matching_signal_returns_warning(self):
        """When no WorkflowSignalMapping has the same name as the input's
        contract_key, the view should add a warning message and refresh.

        The warning tells the author to create a matching signal first,
        giving them the exact name they need.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step, io_definition = _create_step_with_input(workflow)

        url = _auto_link_url(workflow, step, io_definition)
        response = self.client.post(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Refresh"], "true")

        msgs = list(get_messages(response.wsgi_request))
        self.assertEqual(len(msgs), 1)
        self.assertIn("panel_area", str(msgs[0]))
        self.assertIn("No matching", str(msgs[0]))

        # No binding should have been created.
        self.assertFalse(
            StepInputBinding.objects.filter(
                workflow_step=step,
                io_definition=io_definition,
            ).exists(),
        )


# ── Access control ────────────────────────────────────────────────────
# The auto-link view requires manage (AUTHOR) permission on the workflow.


class TestAutoLinkAccessControl(TestCase):
    """Permission and scoping checks for the auto-link view."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_requires_manage_permission(self):
        """A user without AUTHOR role should get a 403 response.

        The auto-link button mutates step input bindings, which is an
        authoring action. Executors and viewers must not be able to
        trigger it.
        """
        workflow = WorkflowFactory()
        step, io_definition = _create_step_with_input(workflow)

        # Log in as the workflow owner but with only EXECUTOR role.
        membership = workflow.user.memberships.get(org=workflow.org)
        membership.set_roles({RoleCode.EXECUTOR})
        workflow.user.set_current_org(workflow.org)
        self.client.force_login(workflow.user)
        session = self.client.session
        session["active_org_id"] = workflow.org_id
        session.save()

        url = _auto_link_url(workflow, step, io_definition)
        response = self.client.post(url)

        self.assertEqual(response.status_code, 403)

    def test_definition_from_other_step_returns_404(self):
        """Using a io_definition_id that belongs to a different step (not the
        step in the URL) must return 404.

        This prevents manipulating bindings on steps the URL doesn't
        reference, which would be a horizontal privilege escalation.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step, _io_definition = _create_step_with_input(workflow)

        # Create a second step with its own I/O definition.
        validator2 = ValidatorFactory(
            org=workflow.org,
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        other_step = WorkflowStepFactory(
            workflow=workflow,
            validator=validator2,
            order=20,
        )
        other_io_definition = StepIODefinition.objects.create(
            workflow_step=other_step,
            contract_key="other_input",
            direction=StepIODirection.INPUT,
        )

        # Try to auto-link the other definition via the first step's URL.
        url = reverse(
            "workflows:workflow_step_io_auto_link",
            kwargs={
                "pk": workflow.pk,
                "step_id": step.pk,
                "io_definition_id": other_io_definition.pk,
            },
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, 404)

    def test_nonexistent_definition_returns_404(self):
        """A bogus io_definition_id must return 404.

        Guards against enumeration and ensures the view doesn't crash
        on invalid PKs.
        """
        workflow = WorkflowFactory()
        _login_as_author(self.client, workflow)
        step, _io_definition = _create_step_with_input(workflow)

        url = reverse(
            "workflows:workflow_step_io_auto_link",
            kwargs={
                "pk": workflow.pk,
                "step_id": step.pk,
                "io_definition_id": 99999,
            },
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, 404)
