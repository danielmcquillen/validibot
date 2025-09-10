from django.apps import AppConfig


class ValidationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "roscoe.validations"

    def ready(self):
        from roscoe.validations.engines import json  # noqa: F401
        from roscoe.validations.engines import xml  # noqa: F401
