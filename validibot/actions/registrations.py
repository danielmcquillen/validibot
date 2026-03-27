"""Built-in action descriptor registrations for the community app.

This module registers the actions that ship with community Validibot.
Commercial packages register their own action descriptors from their own
AppConfig.ready() methods, following the same pattern.
"""

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import IntegrationActionType
from validibot.actions.forms import SlackMessageActionForm
from validibot.actions.handlers import SlackMessageActionHandler
from validibot.actions.models import SlackMessageAction
from validibot.actions.registry import ActionDescriptor
from validibot.actions.registry import register_action_descriptor


def register_builtin_actions() -> None:
    """Register the built-in community action descriptors."""

    register_action_descriptor(
        ActionDescriptor(
            slug="integration-slack-message",
            name="Slack message",
            description="Send a message to a Slack channel.",
            icon="bi-slack",
            action_category=ActionCategoryType.INTEGRATION,
            type=IntegrationActionType.SLACK_MESSAGE,
            model=SlackMessageAction,
            form=SlackMessageActionForm,
            handler=SlackMessageActionHandler,
            provider=__name__,
        ),
    )
