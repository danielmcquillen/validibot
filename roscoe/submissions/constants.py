from django.db import models
from django.utils.translation import gettext_lazy as _


class SubmissionFileType(models.TextChoices):
    JSON = "JSON", _("JSON")
    XML = "XML", _("XML")
    ENERGYPLUS_IDF = "ENERGYPLUS_IDF", _("EnergyPlus IDF File")
    UNKNOWN = "UNKNOWN", _("Unknown")
