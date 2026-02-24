"""
Signal extraction from a parsed ThermModel.

Signals are key-value pairs that become:
1. The payload for this step's assertion evaluation
2. Available to downstream workflow steps via cross-step signal access

Signal values are read from the XML, not computed by a thermal solver.

TODO: Define the signal catalog during implementation based on
what values are available in the THERM XML schema and which are
useful for downstream assertions (e.g., NFRC compliance).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from validibot.validations.validators.therm.models import ThermModel


def extract_signals(model: ThermModel) -> dict[str, Any]:
    """
    Extract all signals from a parsed ThermModel.

    Returns a flat dict of signal_slug -> value pairs matching the
    catalog entries defined in seeds/therm.py.

    TODO: Implement signal extraction. Expected categories:
    - Element counts (polygons, materials, BCs)
    - BC temperatures and film coefficients
    - U-factor tag names
    - Mesh parameters
    - File metadata (version, flags)
    """
    # TODO: implement signal extraction from ThermModel
    return {}
