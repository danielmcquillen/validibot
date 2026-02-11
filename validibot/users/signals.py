from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from validibot.users.models import ensure_personal_workspace


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_personal_workspace(sender, instance, created, **kwargs):
    if not created:
        return
    transaction.on_commit(lambda: ensure_personal_workspace(instance))
