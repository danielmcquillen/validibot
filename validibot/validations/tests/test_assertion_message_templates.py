"""Tests for assertion finding message-template rendering.

Constants exposed the gap this suite protects: the evaluator context already
contained ``c.*``, but the finding message renderer only supported flat names
like ``{{ actual }}``. These tests pin the small template language separately
from any particular validator so BASIC and CEL can share one contract.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from validibot.validations.assertions.message_templates import MessageValueDisplay
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

    def test_known_quantity_uses_catalog_precision_and_unit(self):
        """Measured values should carry their declared unit into findings.

        Keeping this at the rendering boundary lets the evaluator continue to
        compare raw numbers while submitters see an unambiguous, rounded value.
        """

        display = MessageValueDisplay(unit="kWh/m²", precision=2)
        rendered = render_assertion_message_template(
            "EUI was {{ actual }}; target {{ expected }}",
            {"actual": 452.2485348642507, "expected": 0.5},
            value_displays={"actual": display, "expected": display},
        )

        self.assertEqual(rendered, "EUI was 452.25 kWh/m²; target 0.50 kWh/m²")

    def test_round_filter_keeps_unit_and_controls_precision(self):
        """An author's explicit round filter must not discard quantity units."""

        rendered = render_assertion_message_template(
            "EUI was {{ actual | round(1) }}",
            {"actual": 452.2485348642507},
            value_displays={
                "actual": MessageValueDisplay(unit="kWh/m²", precision=2),
            },
        )

        self.assertEqual(rendered, "EUI was 452.2 kWh/m²")
