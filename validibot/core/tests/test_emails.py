"""Tests for the periodic email handler registry."""

from io import StringIO

from django.test import SimpleTestCase

from validibot.core.emails import get_periodic_email_handlers
from validibot.core.emails import register_periodic_email_handler
from validibot.core.emails import reset_periodic_email_handlers


class PeriodicEmailRegistryTests(SimpleTestCase):
    """Tests for register/get/reset periodic email handlers."""

    def setUp(self):
        reset_periodic_email_handlers()

    def tearDown(self):
        reset_periodic_email_handlers()

    def test_register_and_get(self):
        def my_handler(stdout: StringIO) -> None:
            pass

        register_periodic_email_handler("test-handler", my_handler)
        handlers = get_periodic_email_handlers()

        self.assertIn("test-handler", handlers)
        self.assertIs(handlers["test-handler"], my_handler)

    def test_get_returns_copy(self):
        """get_periodic_email_handlers should return a copy, not the internal dict."""

        def handler(stdout: StringIO) -> None:
            pass

        register_periodic_email_handler("test", handler)
        handlers = get_periodic_email_handlers()
        handlers.clear()

        self.assertEqual(len(get_periodic_email_handlers()), 1)

    def test_empty_returns_empty_dict(self):
        handlers = get_periodic_email_handlers()
        self.assertEqual(handlers, {})

    def test_reset_clears_all(self):
        def handler(stdout: StringIO) -> None:
            pass

        register_periodic_email_handler("a", handler)
        register_periodic_email_handler("b", handler)
        reset_periodic_email_handlers()

        self.assertEqual(get_periodic_email_handlers(), {})

    def test_reregistration_is_idempotent(self):
        """Registering the same name twice replaces the previous handler."""

        def handler1(stdout: StringIO) -> None:
            pass

        def handler2(stdout: StringIO) -> None:
            pass

        register_periodic_email_handler("test", handler1)
        register_periodic_email_handler("test", handler2)

        handlers = get_periodic_email_handlers()
        self.assertEqual(len(handlers), 1)
        self.assertIs(handlers["test"], handler2)
