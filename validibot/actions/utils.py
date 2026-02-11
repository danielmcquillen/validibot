import logging

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import CertificationActionType
from validibot.actions.constants import IntegrationActionType
from validibot.actions.models import Action
from validibot.actions.models import ActionDefinition

logger = logging.getLogger(__name__)

DEFAULT_ACTION_DEFINITIONS = [
    {
        "slug": "integration-slack-message",
        "name": "Slack message",
        "description": "Send a message to a Slack channel.",
        "icon": "bi-slack",
        "action_category": ActionCategoryType.INTEGRATION,
        "type": IntegrationActionType.SLACK_MESSAGE,
    },
    {
        "slug": "certification-signed-certificate",
        "name": "Signed certificate",
        "description": "Issue a signed certificate for successful validations.",
        "icon": "bi-award",
        "action_category": ActionCategoryType.CERTIFICATION,
        "type": CertificationActionType.SIGNED_CERTIFICATE,
    },
]


def create_default_actions() -> tuple[list[Action], list[Action]]:
    created = []
    skipped = []

    for definition in DEFAULT_ACTION_DEFINITIONS:
        obj, was_created = ActionDefinition.objects.get_or_create(
            slug=definition["slug"],
            defaults={
                "name": definition["name"],
                "description": definition["description"],
                "icon": definition["icon"],
                "action_category": definition["action_category"],
                "type": definition["type"],
            },
        )
        if was_created:
            created.append(obj)
            logger.info("Created default action definition: %s", obj.slug)
        else:
            skipped.append(obj)
    if created:
        logger.info(
            f"Created {obj} default action definitions.",
        )
    else:
        logger.info("Default action definitions already exist.")

    return created, skipped
