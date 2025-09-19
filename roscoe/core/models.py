from django.db import models
from django_extensions.db.models import TimeStampedModel

# Create your models here.
from roscoe.users.models import User


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
