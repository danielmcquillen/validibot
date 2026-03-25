import logging

from django.core.management.base import BaseCommand

from validibot.actions.utils import create_default_actions

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Sync ``ActionDefinition`` rows from registered action descriptors."""

    help = "Sync action definitions from registered action plugins (idempotent)."

    def handle(self, *args, **options):
        created, skipped = create_default_actions()
