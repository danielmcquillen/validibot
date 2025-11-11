from django.db import models
from django.utils.translation import gettext_lazy as _


class SubmissionFileType(models.TextChoices):
    """
    The type of submission file supported by a workflow or validator.
    """

    JSON = "json", _("JSON")
    XML = "xml", _("XML")
    TEXT = "text", _("Plain Text")
    YAML = "yaml", _("YAML")
    BINARY = "binary", _("Binary")
    UNKNOWN = "UNKNOWN", _("Unknown")
