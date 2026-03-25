from __future__ import annotations

from importlib import import_module

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import IntegrationActionType
from validibot.actions.forms import SlackMessageActionForm
from validibot.actions.models import Action
from validibot.actions.models import ActionDefinition
from validibot.actions.models import SlackMessageAction
from validibot.actions.registry import get_action_form
from validibot.actions.registry import get_action_model
from validibot.actions.utils import create_default_actions
from validibot.workflows.models import WorkflowStep
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def test_seed_default_actions_creates_definitions():
    """Seeding creates the registered community action catalog exactly once."""
    assert ActionDefinition.objects.count() == 0

    call_command("seed_default_actions")

    definitions = ActionDefinition.objects.all()
    assert definitions.filter(action_category=ActionCategoryType.INTEGRATION).exists()
    initial_count = definitions.count()

    call_command("seed_default_actions")

    assert ActionDefinition.objects.count() == initial_count


def test_create_default_actions_updates_existing_definition_metadata():
    """Descriptor sync should update stale metadata on existing rows."""

    definition = ActionDefinition.objects.create(
        slug="integration-slack-message",
        name="Old Slack",
        description="Old description",
        icon="bi-x",
        action_category=ActionCategoryType.INTEGRATION,
        type=IntegrationActionType.SLACK_MESSAGE,
    )

    created, updated = create_default_actions()

    assert created == []
    assert updated == [definition]

    definition.refresh_from_db()
    assert definition.name == "Slack message"
    assert definition.description == "Send a message to a Slack channel."
    assert definition.icon == "bi-slack"


def test_action_registry_resolves_variants():
    """The community action registry resolves only installed action plugins."""
    assert get_action_model(IntegrationActionType.SLACK_MESSAGE) is SlackMessageAction
    assert (
        get_action_form(IntegrationActionType.SLACK_MESSAGE) is SlackMessageActionForm
    )
    assert get_action_model("SIGNED_CREDENTIAL") is Action
    assert get_action_form("SIGNED_CREDENTIAL") is None


def test_rename_migration_updates_legacy_step_config():
    """The rename migration rewrites old action definitions and step config keys."""
    workflow = WorkflowFactory()
    definition = ActionDefinition.objects.create(
        slug="certification-signed-certificate",
        name="Signed certificate",
        description="Issue a signed certificate for successful validations.",
        icon="bi-award",
        action_category=ActionCategoryType.CERTIFICATION,
        type="SIGNED_CERTIFICATE",
    )
    action = Action.objects.create(
        definition=definition,
        name="Legacy credential step",
        description="",
    )
    step = WorkflowStep.objects.create(
        workflow=workflow,
        action=action,
        order=10,
        name="Legacy credential step",
        description="",
        config={
            "certificate_template": "legacy.pdf",
            "preserved_key": "keep-this",
        },
    )

    migration = import_module(
        "validibot.actions.migrations.0002_rename_signed_certificate_to_credential",
    )
    migration.rename_certificate_to_credential(django_apps, None)

    definition.refresh_from_db()
    step.refresh_from_db()

    assert definition.type == "SIGNED_CREDENTIAL"
    assert definition.slug == "certification-signed-credential"
    assert definition.name == "Signed credential"
    assert definition.description == (
        "Issue a signed credential for successful validations."
    )
    assert step.config == {
        "credential_template": "legacy.pdf",
        "preserved_key": "keep-this",
    }
