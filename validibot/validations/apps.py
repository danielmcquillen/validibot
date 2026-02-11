import logging
import os

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class ValidationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.validations"

    def ready(self):
        # Import engines to register them
        from validibot.validations.engines import energyplus  # noqa: F401
        from validibot.validations.engines import fmi  # noqa: F401
        from validibot.validations.engines import json  # noqa: F401
        from validibot.validations.engines import xml  # noqa: F401

        # Run startup container cleanup in worker processes
        # Skip in web server, management commands, and autoreload subprocesses
        if self._should_run_startup_cleanup():
            self._run_startup_cleanup()

    def _should_run_startup_cleanup(self) -> bool:
        """
        Determine if startup container cleanup should run.

        Only runs in Celery worker processes on Docker Compose deployments.
        Skips in web servers, management commands, and tests.
        """
        from django.conf import settings

        # Only run on Docker Compose deployments
        deployment_target = getattr(settings, "DEPLOYMENT_TARGET", "")
        if deployment_target not in ("docker_compose", "local_docker_compose"):
            return False

        # Check if we're in a Celery worker process
        # Check for celery in sys.argv (e.g., "celery -A config worker")
        import sys

        if not any("celery" in arg for arg in sys.argv):
            return False

        # Skip in Django's autoreload subprocess
        return os.environ.get("RUN_MAIN") != "true"

    def _run_startup_cleanup(self) -> None:
        """
        Clean up orphaned containers from previous worker runs.

        This runs once at worker startup to remove containers that may
        have been left behind if the previous worker crashed.
        """
        try:
            from validibot.validations.services.runners.docker import (
                cleanup_all_managed_containers,
            )

            logger.info("Running startup cleanup of orphaned containers...")
            removed, failed = cleanup_all_managed_containers()

            if removed > 0:
                logger.info("Startup cleanup removed %d orphaned container(s)", removed)
            if failed > 0:
                logger.warning(
                    "Startup cleanup failed to remove %d container(s)", failed
                )
        except ImportError:
            logger.debug("Docker package not installed, skipping startup cleanup")
        except Exception:
            logger.exception("Startup container cleanup failed")
