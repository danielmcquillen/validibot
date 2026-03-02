"""
Management command to run registered periodic email handlers.

This is the community-side orchestrator for periodic emails. Downstream
packages (e.g., validibot-cloud) register handlers at app-ready time via
``register_periodic_email_handler()``. This command calls each registered
handler in sequence.

In community-only installs, no handlers are registered and this is a no-op.

Usage:
    python manage.py send_periodic_emails
"""

from django.core.management.base import BaseCommand

from validibot.core.emails import get_periodic_email_handlers


class Command(BaseCommand):
    help = "Run registered periodic email handlers."

    def handle(self, *args, **options):
        handlers = get_periodic_email_handlers()
        if not handlers:
            self.stdout.write("No periodic email handlers registered. Skipping.")
            return

        for name, handler in handlers.items():
            self.stdout.write(f"Running: {name}")
            handler(self.stdout)
            self.stdout.write(f"Completed: {name}")
