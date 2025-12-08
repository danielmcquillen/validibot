from django.db import models
from model_utils.models import TimeStampedModel

# Create your models here.
from validibot.users.models import User


class SupportMessage(TimeStampedModel):
    """
    Simple model to hold user support messages.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="support_messages",
    )
    subject = models.CharField(max_length=1000)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.subject


class SiteSettings(TimeStampedModel):
    """
    Singleton-style container for platform-wide configuration.

    A single row (slugged as ``default``) stores a JSON document that is loaded
    into typed settings objects elsewhere in the codebase. System administrators
    manage these values via the Django admin.
    """

    DEFAULT_SLUG = "default"

    slug = models.SlugField(
        max_length=100,
        unique=True,
        default=DEFAULT_SLUG,
        help_text="Identifier for this settings record. Only 'default' is used.",
    )
    data = models.JSONField(
        default=dict,
        blank=True,
        help_text="JSON payload containing namespaced site configuration.",
    )

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def __str__(self):
        return f"SiteSettings<{self.slug}>"
