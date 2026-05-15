"""
Regression tests for private-by-default Django media storage.

Cloud deployments expose the ``public/`` bucket prefix directly for avatars
and workflow images. Customer data must therefore use the private default
storage unless a model field explicitly asks for public media.
"""

from django.core.files.storage import storages

from validibot.users.models import select_public_storage as user_public_storage
from validibot.workflows.models import select_public_storage as workflow_public_storage


def test_default_and_public_storage_are_distinct_aliases():
    """A public media alias must exist so default storage can stay private."""
    assert storages["default"] is not storages["public"]


def test_public_model_helpers_use_public_storage_alias():
    """Avatar and workflow image fields should opt in to public storage."""
    assert user_public_storage() is storages["public"]
    assert workflow_public_storage() is storages["public"]
