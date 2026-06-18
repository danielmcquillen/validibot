"""Collision-storm guard for ``unique_ruleset_name`` (ADR 04-23 §hyg.unbounded_while).

``unique_ruleset_name`` appends an incrementing numeric suffix until it finds
an unused ruleset name, with a DB query per iteration. Without a cap, input
that seeds many colliding names could spin that loop indefinitely. The fix
caps numeric probing and falls back to a one-shot UUID suffix.
"""

from __future__ import annotations

from unittest.mock import patch

from validibot.workflows.views_helpers import unique_ruleset_name

UUID_SUFFIX_LEN = 8


def test_terminates_with_uuid_suffix_under_collision_storm():
    """When every candidate name collides, the call must terminate.

    We force ``Ruleset.objects.filter(...).exists()`` to always report a
    collision (simulating a pathological / malicious set of seeded names).
    The function must not hang: past the numeric cap it returns a
    UUID-suffixed name in a single step.
    """
    with patch("validibot.workflows.views_helpers.Ruleset.objects") as objects:
        objects.filter.return_value.exists.return_value = True
        name = unique_ruleset_name(
            org=None,  # only forwarded to the (mocked) filter; never queried
            ruleset_type="energyplus",
            base_name="leak",
            version="1",
        )

    assert name.startswith("leak-")
    suffix = name.rsplit("-", 1)[1]
    assert len(suffix) == UUID_SUFFIX_LEN  # UUID hex, not a small integer
