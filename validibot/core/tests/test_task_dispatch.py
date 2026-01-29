"""
Tests for the task dispatch system.
"""

import pytest
from django.test import TestCase
from django.test import override_settings

from validibot.core.tasks.dispatch import TaskDispatchRequest
from validibot.core.tasks.dispatch import clear_dispatcher_cache
from validibot.core.tasks.dispatch import get_task_dispatcher


class TaskDispatcherRegistryTests(TestCase):
    """Tests for the dispatcher registry and factory."""

    def setUp(self):
        # Clear cache before each test
        clear_dispatcher_cache()

    def tearDown(self):
        # Clear cache after each test
        clear_dispatcher_cache()

    def test_default_dispatcher_in_tests_is_test_dispatcher(self):
        """Test environment (DEPLOYMENT_TARGET=test) should get TestDispatcher."""
        dispatcher = get_task_dispatcher()
        self.assertEqual(dispatcher.dispatcher_name, "test")
        self.assertTrue(dispatcher.is_sync)

    @override_settings(DEPLOYMENT_TARGET="test")
    def test_test_deployment_target(self):
        """DEPLOYMENT_TARGET=test should use TestDispatcher."""
        dispatcher = get_task_dispatcher()
        self.assertEqual(dispatcher.dispatcher_name, "test")
        self.assertTrue(dispatcher.is_sync)

    @override_settings(DEPLOYMENT_TARGET="local_docker_compose")
    def test_local_docker_compose_deployment_target(self):
        """DEPLOYMENT_TARGET=local_docker_compose should use LocalDevDispatcher."""
        dispatcher = get_task_dispatcher()
        self.assertEqual(dispatcher.dispatcher_name, "local_dev")
        self.assertTrue(dispatcher.is_sync)

    @override_settings(DEPLOYMENT_TARGET="docker_compose")
    def test_docker_compose_deployment_target(self):
        """DEPLOYMENT_TARGET=docker_compose should use DramatiqDispatcher."""
        dispatcher = get_task_dispatcher()
        self.assertEqual(dispatcher.dispatcher_name, "dramatiq")
        self.assertFalse(dispatcher.is_sync)

    @override_settings(DEPLOYMENT_TARGET="gcp")
    def test_gcp_deployment_target(self):
        """DEPLOYMENT_TARGET=gcp should use GoogleCloudTasksDispatcher."""
        dispatcher = get_task_dispatcher()
        self.assertEqual(dispatcher.dispatcher_name, "cloud_tasks")
        self.assertFalse(dispatcher.is_sync)

    @override_settings(DEPLOYMENT_TARGET="invalid_target")
    def test_invalid_deployment_target_raises_error(self):
        """Invalid DEPLOYMENT_TARGET should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid DEPLOYMENT_TARGET"):
            get_task_dispatcher()

    @override_settings(DEPLOYMENT_TARGET=None)
    def test_missing_deployment_target_raises_error(self):
        """Missing DEPLOYMENT_TARGET should raise ValueError."""
        with pytest.raises(ValueError, match="DEPLOYMENT_TARGET setting is required"):
            get_task_dispatcher()


class TaskDispatchRequestTests(TestCase):
    """Tests for TaskDispatchRequest."""

    def test_to_payload_basic(self):
        """Test basic payload conversion."""
        request = TaskDispatchRequest(
            validation_run_id="abc-123",
            user_id=42,
        )
        payload = request.to_payload()

        self.assertEqual(payload["validation_run_id"], "abc-123")
        self.assertEqual(payload["user_id"], 42)
        self.assertIsNone(payload["resume_from_step"])

    def test_to_payload_with_resume_step(self):
        """Test payload conversion with resume_from_step."""
        request = TaskDispatchRequest(
            validation_run_id="abc-123",
            user_id=42,
            resume_from_step=3,
        )
        payload = request.to_payload()

        self.assertEqual(payload["resume_from_step"], 3)

    def test_to_payload_converts_uuid(self):
        """Test that UUIDs are converted to strings."""
        import uuid

        run_id = uuid.uuid4()
        request = TaskDispatchRequest(
            validation_run_id=run_id,
            user_id=1,
        )
        payload = request.to_payload()

        self.assertEqual(payload["validation_run_id"], str(run_id))
        self.assertIsInstance(payload["validation_run_id"], str)
