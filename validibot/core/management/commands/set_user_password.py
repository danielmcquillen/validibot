"""
One-off command to set a user's password non-interactively.

Usage:
    python manage.py set_user_password <username> <password>

This is useful for Cloud Run Jobs where interactive input isn't available.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

User = get_user_model()


class Command(BaseCommand):
    """Set a user's password from the command line."""

    help = "Set a user's password non-interactively"

    def add_arguments(self, parser):
        parser.add_argument("username", type=str, help="Username of the user")
        parser.add_argument("password", type=str, help="New password to set")

    def handle(self, *args, **options):
        username = options["username"]
        password = options["password"]

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist as dne:
            raise CommandError(f"User '{username}' does not exist") from dne

        user.set_password(password)
        user.save()

        self.stdout.write(
            self.style.SUCCESS(f"Password updated for user '{username}'"),
        )
