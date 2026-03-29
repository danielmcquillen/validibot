import logging

from django.apps import AppConfig

from validibot.validations.services.cleanup import run_startup_cleanup
from validibot.validations.services.cleanup import should_run_startup_cleanup

logger = logging.getLogger(__name__)


class ValidationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.validations"

    def ready(self):
        # Register community validator configs during app startup.
        # Commercial apps follow the same pattern from their own
        # AppConfig.ready() methods using register_validator_config().
        from validibot.validations.registrations import register_validators

        register_validators()

        # Run startup container cleanup in worker processes
        # Skip in web server, management commands, and autoreload subprocesses
        if should_run_startup_cleanup():
            run_startup_cleanup()
