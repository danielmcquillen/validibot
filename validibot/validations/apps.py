from django.apps import AppConfig


class ValidationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.validations"

    def ready(self):
        # Import engines to register them
        from validibot.validations.engines import energyplus  # noqa: F401
        from validibot.validations.engines import fmi  # noqa: F401
        from validibot.validations.engines import json  # noqa: F401
        from validibot.validations.engines import xml  # noqa: F401
