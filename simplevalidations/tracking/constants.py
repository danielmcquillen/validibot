from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _


class TrackingEventType(TextChoices):
    """
    Enum for tracking event types.
    """

    PAGE_VIEW = "PAGE_VIEW", _("Page View")
    BUTTON_CLICK = "BUTTON_CLICK", _("Button Click")
    FORM_SUBMIT = "FORM_SUBMIT", _("Form Submit")
    FILE_DOWNLOAD = "FILE_DOWNLOAD", "File Download"
    CUSTOM_EVENT = "CUSTOM_EVENT", "Custom Event"
    APP_EVENT = "APP_EVENT", _("Application Event")
