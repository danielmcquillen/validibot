"""
Envelope loader utilities for validator containers.

Provides helpers for loading input envelopes from environment variables or
command-line arguments.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TypeVar

from pydantic import BaseModel

from validators.shared.gcs_client import download_envelope

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def load_input_envelope[T: BaseModel](envelope_class: type[T]) -> T:
    """
    Load input envelope from environment variable or command-line argument.

    Checks for input URI in this order:
    1. INPUT_URI environment variable
    2. First command-line argument (sys.argv[1])

    Args:
        envelope_class: Pydantic model class to deserialize to

    Returns:
        Deserialized envelope instance

    Raises:
        ValueError: If no input URI is provided
        ValidationError: If JSON doesn't match envelope schema
    """
    # Check environment variable first
    input_uri = os.getenv("INPUT_URI")

    # Fall back to command-line argument
    if not input_uri and len(sys.argv) > 1:
        input_uri = sys.argv[1]

    if not input_uri:
        raise ValueError(
            "No input URI provided. Set INPUT_URI environment variable "
            "or pass as first argument."
        )

    logger.info("Loading input envelope from %s", input_uri)

    return download_envelope(input_uri, envelope_class)


def get_output_uri(input_envelope: BaseModel) -> str:
    """
    Get the output.json URI from input envelope's execution context.

    Args:
        input_envelope: Input envelope with context.execution_bundle_uri

    Returns:
        GCS URI where output.json should be uploaded

    Raises:
        AttributeError: If envelope doesn't have expected structure
    """
    # Get execution bundle URI from context
    execution_bundle_uri = input_envelope.context.execution_bundle_uri

    # Ensure it ends with /
    if not execution_bundle_uri.endswith("/"):
        execution_bundle_uri += "/"

    output_uri = f"{execution_bundle_uri}output.json"

    logger.info("Output envelope will be uploaded to %s", output_uri)

    return output_uri
