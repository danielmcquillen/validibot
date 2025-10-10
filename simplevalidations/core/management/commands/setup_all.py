# Custom command to set up default forums and forum permissions
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from simplevalidations.users.models import User

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Set up initial data and settings for PureLMS.
    """

    def __init__(self, stdout=None, stderr=None, no_color=False):
        super().__init__(stdout=stdout, stderr=stderr, no_color=no_color)

    def handle(self, *args, **options):
        self.stdout.write("Setting up SimpleValidations.")
        self.stdout.write("  ")
        self._setup_local_superuser()
        self.stdout.write("DONE setting up SimpleValidations")

    def _setup_local_superuser(self):
        """
        Set up a local superuser for development.
        """
        username = getattr(settings, "SUPERUSER_USERNAME", None)
        password = getattr(settings, "SUPERUSER_PASSWORD", None)
        email = getattr(settings, "SUPERUSER_EMAIL", None)
        name = getattr(settings, "SUPERUSER_NAME", None)

        if not username:
            return
        if User.objects.filter(username=username).exists():
            return

        logger.info(f"Creating user '{username}'")

        user = User.objects.create_user(
            username=username, email=email, password=password
        )
        user.name = name
        user.is_staff = True
        user.is_superuser = True
        user.save()

        # Create email for django-allauth
        user.emailaddress_set.create(email=email, primary=True, verified=True)
