from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _


class TrackingEventType(TextChoices):
    """
    Enum for tracking event types.
    """

    PAGE_VIEW = "page_view", _("Page View")
    BUTTON_CLICK = "button_click", _("Button Click")
    FORM_SUBMIT = "form_submit", _("Form Submit")
    FILE_DOWNLOAD = "file_download", "File Download"
    CUSTOM_EVENT = "custom_event", "Custom Event"
