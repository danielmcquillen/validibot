from django.apps import AppConfig


class ValidationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "simplevalidations.validations"

    def ready(self):
        from simplevalidations.validations.engines import json  # noqa: F401
        from simplevalidations.validations.engines import xml  # noqa: F401
