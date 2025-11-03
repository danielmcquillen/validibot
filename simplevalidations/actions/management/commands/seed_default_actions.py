from django.core.management.base import BaseCommand

from simplevalidations.actions.constants import (
    ActionCategoryType,
    CertificationActionType,
    IntegrationActionType,
)
from simplevalidations.actions.models import ActionDefinition


class Command(BaseCommand):
    """Seed the default integration and certification action definitions."""

    help = "Create default action definitions (idempotent)."

    DEFAULT_DEFINITIONS = [
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

    def handle(self, *args, **options):
        created = 0
        for definition in self.DEFAULT_DEFINITIONS:
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
                created += 1
        if created:
            self.stdout.write(
                self.style.SUCCESS(f"Created {created} default action definitions."),
            )
        else:
            self.stdout.write("Default action definitions already exist.")
