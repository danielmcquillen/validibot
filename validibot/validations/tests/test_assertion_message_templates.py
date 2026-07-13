"""Tests for assertion finding message-template rendering.

Constants exposed the gap this suite protects: the evaluator context already
contained ``c.*``, but the finding message renderer only supported flat names
like ``{{ actual }}``. These tests pin the small template language separately
from any particular validator so BASIC and CEL can share one contract.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from validibot.validations.assertions.message_templates import (
    render_assertion_message_template,
)


class AssertionMessageTemplateRenderingTests(SimpleTestCase):
    """Verify the shared assertion message-template contract."""

    def test_dotted_namespace_lookup_interpolates_constants(self):
        """A workflow constant can be referenced as ``{{ c.name }}`` in messages."""
        rendered = render_assertion_message_template(
            "Not the same as bubba's value {{ c.bubba }}",
            {"c": {"bubba": "dance"}},
        )

        self.assertEqual(rendered, "Not the same as bubba's value dance")

    def test_case_mismatch_leaves_unknown_constant_literal(self):
        """Constant names are case-sensitive, matching CEL identifier lookup."""
        rendered = render_assertion_message_template(
            "Not the same as bubba's value {{ c.Bubba }}",
            {"c": {"bubba": "dance"}},
        )

        self.assertEqual(rendered, "Not the same as bubba's value {{ c.Bubba }}")

    def test_existing_flat_variables_and_filters_still_render(self):
        """Existing ``{{ actual }}`` templates keep their flat-key/filter behavior."""
        rendered = render_assertion_message_template(
            "Price {{ actual | round(1) }} exceeds {{ value }}",
            {"actual": 25.04, "value": 20},
        )

        self.assertEqual(rendered, "Price 25.0 exceeds 20")
