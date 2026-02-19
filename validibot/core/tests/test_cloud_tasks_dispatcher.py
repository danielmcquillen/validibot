"""
Tests for GoogleCloudTasksDispatcher service account configuration.
"""

import pytest
from django.test.utils import override_settings

from validibot.core.tasks.dispatch.google_cloud_tasks import GoogleCloudTasksDispatcher


def test_get_invoker_service_account_uses_explicit_setting():
    """Use the explicit CLOUD_TASKS_SERVICE_ACCOUNT when configured."""
    dispatcher = GoogleCloudTasksDispatcher()

    with override_settings(
        CLOUD_TASKS_SERVICE_ACCOUNT="invoker@example.com",
    ):
        assert dispatcher._get_invoker_service_account() == "invoker@example.com"


def test_get_invoker_service_account_requires_setting():
    """Raise a clear error when CLOUD_TASKS_SERVICE_ACCOUNT is not set."""
    dispatcher = GoogleCloudTasksDispatcher()

    with (
        override_settings(CLOUD_TASKS_SERVICE_ACCOUNT=""),
        pytest.raises(
            ValueError,
            match="CLOUD_TASKS_SERVICE_ACCOUNT must be set",
        ),
    ):
        dispatcher._get_invoker_service_account()
