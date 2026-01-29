"""
Tests for GoogleCloudTasksDispatcher service account configuration.
"""

import pytest
from core.tasks.dispatch.google_cloud_tasks import GoogleCloudTasksDispatcher
from django.test.utils import override_settings


def test_get_invoker_service_account_prefers_explicit_setting():
    """Prefer an explicit Cloud Tasks OIDC service account when configured."""
    dispatcher = GoogleCloudTasksDispatcher()

    with override_settings(
        CLOUD_TASKS_SERVICE_ACCOUNT="invoker@example.com",
        GCP_PROJECT_ID="project-x",
    ):
        assert dispatcher._get_invoker_service_account() == "invoker@example.com"


def test_get_invoker_service_account_falls_back_to_appspot_service_account():
    """Fall back to the App Engine default SA only when an explicit value is absent."""
    dispatcher = GoogleCloudTasksDispatcher()

    with override_settings(
        CLOUD_TASKS_SERVICE_ACCOUNT="",
        GCP_PROJECT_ID="project-x",
    ):
        assert (
            dispatcher._get_invoker_service_account()
            == "project-x@appspot.gserviceaccount.com"
        )


def test_get_invoker_service_account_requires_some_configuration():
    """Raise a clear error when neither service account nor project ID is configured."""
    dispatcher = GoogleCloudTasksDispatcher()

    with (
        override_settings(
            CLOUD_TASKS_SERVICE_ACCOUNT="",
            GCP_PROJECT_ID="",
        ),
        pytest.raises(
            ValueError,
            match="CLOUD_TASKS_SERVICE_ACCOUNT or GCP_PROJECT_ID must be set",
        ),
    ):
        dispatcher._get_invoker_service_account()
