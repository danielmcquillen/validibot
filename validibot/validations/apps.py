from django.apps import AppConfig


class ValidationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.validations"

    def ready(self):
        from validibot.validations.engines import energyplus  # noqa: F401
        from validibot.validations.engines import fmi  # noqa: F401
        from validibot.validations.engines import json  # noqa: F401
        from validibot.validations.engines import xml  # noqa: F401
        from validibot.validations.providers import (
            energyplus as provider_energyplus,  # noqa: F401
        )
        from validibot.validations.providers import fmi as provider_fmi  # noqa: F401
