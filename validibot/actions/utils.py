import logging

from validibot.actions.models import ActionDefinition
from validibot.actions.registry import get_action_descriptors

logger = logging.getLogger(__name__)


def create_default_actions() -> tuple[list[ActionDefinition], list[ActionDefinition]]:
    """Sync ``ActionDefinition`` rows from the registered action plugins."""

    created = []
    updated = []

    for descriptor in get_action_descriptors():
        obj, was_created = ActionDefinition.objects.update_or_create(
            slug=descriptor.slug,
            defaults={
                "name": descriptor.name,
                "description": descriptor.description,
                "icon": descriptor.icon,
                "action_category": descriptor.action_category,
                "type": descriptor.type,
                "required_feature": descriptor.required_feature,
            },
        )
        if was_created:
            created.append(obj)
            logger.info("Created default action definition: %s", obj.slug)
        else:
            updated.append(obj)
            logger.info("Updated default action definition: %s", obj.slug)
    if created:
        logger.info(
            f"Created {obj} default action definitions.",
        )
    elif updated:
        logger.info("Default action definitions updated from registered plugins.")
    else:
        logger.info("Default action definitions already exist.")

    return created, updated
