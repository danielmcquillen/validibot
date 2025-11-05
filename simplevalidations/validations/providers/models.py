"""
Pydantic data structures shared by validation providers.

These models keep catalog definitions typed and documented so both the
authoring UI and the backend migration helpers can reason about the same
shape.
"""

from __future__ import annotations

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from simplevalidations.validations.constants import CatalogEntryType
from simplevalidations.validations.constants import CatalogValueType


class CatalogEntryDefinition(BaseModel):
    """
    Description of a single catalog entry (signal, derivation, etc.).

    Providers return a list of these models from ``get_catalog_defaults`` and the
    persistence layer materialises them into ``ValidatorCatalogEntry`` rows.
    """

    entry_type: CatalogEntryType = Field(
        ...,
        description="Whether this entry is an input signal, output signal, or derivation.",
    )
    slug: str = Field(..., description="Unique identifier within the validator.")
    label: str = Field(..., description="Human readable label.")
    data_type: CatalogValueType = Field(
        default=CatalogValueType.NUMBER,
        description="Runtime type for the value (number, timeseries, etc.).",
    )
    description: str = Field(
        default="",
        description="Detailed description shown in editors.",
    )
    binding_config: dict = Field(
        default_factory=dict,
        description="Provider-specific binding metadata (source identifiers, paths, etc.).",
    )
    metadata: dict = Field(
        default_factory=dict,
        description="Additional metadata for the UI (units, tags, etc.).",
    )
    is_required: bool = Field(
        default=False,
        description="Whether every ruleset must include this entry.",
    )
    order: int = Field(
        default=0,
        description="Display ordering.",
    )

    model_config = ConfigDict(use_enum_values=True)
