"""
Step output extraction from a parsed ThermModel.

Step output values are key-value pairs that become:
1. The payload for this step's assertion evaluation
2. Available to downstream workflow steps via promotion or the steps namespace

Step output values are read from the XML, not computed by a thermal solver.

TODO: Define the step output catalog during implementation based on
what values are available in the THERM XML schema and which are
useful for downstream assertions (e.g., NFRC compliance).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from validibot.validations.validators.therm.models import ThermModel


def extract_output_values(model: ThermModel) -> dict[str, Any]:
    """
    Extract all output values from a parsed ThermModel.

    Returns a flat dict of output_key -> value pairs matching the
    catalog entries defined in seeds/therm.py.

    TODO: Implement output-value extraction. Expected categories:
    - Element counts (polygons, materials, BCs)
    - BC temperatures and film coefficients
    - U-factor tag names
    - Mesh parameters
    - File metadata (version, flags)
    """
    # TODO: implement output-value extraction from ThermModel
    return {}
