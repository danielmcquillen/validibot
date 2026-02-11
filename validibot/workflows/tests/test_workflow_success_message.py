"""
Tests for Workflow.success_message functionality.

This module tests the custom success message feature that allows workflow
authors to define a custom message displayed when validation succeeds.
"""

from django.test import TestCase

from validibot.projects.tests.factories import ProjectFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.workflows.models import Workflow


class WorkflowSuccessMessageTests(TestCase):
    """
    Tests for the Workflow.success_message field.

    Verifies that:
    1. Workflows can store custom success messages
    2. Empty success_message defaults to blank string
    3. Success message is accessible on the model
    """

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        cls.project = ProjectFactory(org=cls.org)

    def test_workflow_with_custom_success_message(self):
        """
        Workflow can store a custom success message.

        When a workflow has a success_message set, it should be retrievable
        and used in place of the default success message.
        """
        workflow = Workflow.objects.create(
            org=self.org,
            user=self.user,
            name="Test Workflow",
            success_message="Your model passed all validation checks!",
        )

        self.assertEqual(
            workflow.success_message,
            "Your model passed all validation checks!",
        )

    def test_workflow_success_message_defaults_to_blank(self):
        """
        Workflow success_message defaults to empty string.

        When no success_message is set, it should default to an empty string,
        which signals the template to use the default message.
        """
        workflow = Workflow.objects.create(
            org=self.org,
            user=self.user,
            name="Test Workflow",
        )

        self.assertEqual(workflow.success_message, "")

    def test_workflow_success_message_can_be_updated(self):
        """
        Workflow success_message can be updated after creation.

        Workflow authors should be able to add or modify the success message
        at any time.
        """
        workflow = Workflow.objects.create(
            org=self.org,
            user=self.user,
            name="Test Workflow",
        )
        self.assertEqual(workflow.success_message, "")

        workflow.success_message = "Updated success message!"
        workflow.save()
        workflow.refresh_from_db()

        self.assertEqual(workflow.success_message, "Updated success message!")

    def test_workflow_success_message_multiline(self):
        """
        Workflow success_message can contain multiline text.

        Success messages may need to span multiple lines for detailed
        positive feedback.
        """
        multiline_message = """Congratulations!
Your building energy model has passed all validation checks.
You may now proceed to submit for certification."""

        workflow = Workflow.objects.create(
            org=self.org,
            user=self.user,
            name="Test Workflow",
            success_message=multiline_message,
        )

        self.assertEqual(workflow.success_message, multiline_message)
        self.assertIn("Congratulations!", workflow.success_message)
        self.assertIn("certification", workflow.success_message)
