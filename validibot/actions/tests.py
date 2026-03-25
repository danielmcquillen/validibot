from __future__ import annotations

import pytest
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
