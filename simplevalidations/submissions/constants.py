from django.db import models
from django.utils.translation import gettext_lazy as _


class SubmissionFileType(models.TextChoices):
    JSON = "application/json", _("JSON")
    XML = "application/xml", _("XML")
    TEXT = "text/plain", _("Plain Text")
    ENERGYPLUS_IDF = "text/x-idf", _("EnergyPlus IDF File")
    UNKNOWN = "UNKNOWN", _("Unknown")
    # YAML = "application/yaml", _("YAML")
