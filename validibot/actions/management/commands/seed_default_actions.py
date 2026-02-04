import logging

from django.core.management.base import BaseCommand

from validibot.actions.utils import create_default_actions

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Seed the default integration and certification action definitions."""

    help = "Create default action definitions (idempotent)."

    def handle(self, *args, **options):
        created, skipped = create_default_actions()
