"""
Validator runner registry and factory.

This module provides the central access point for getting the configured
validator runner backend. The runner is selected based on the VALIDATOR_RUNNER
setting.

Usage:
    from validibot.validations.services.runners import get_validator_runner

    runner = get_validator_runner()
    execution_id = runner.run(
        container_image="validibot/validator-energyplus:latest",
        input_uri="file:///app/data/runs/run-123/input.json",
    )
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils.module_loading import import_string

if TYPE_CHECKING:
    from validibot.validations.services.runners.base import ValidatorRunner

logger = logging.getLogger(__name__)

# Runner backend class paths
RUNNER_ALIASES = {
    "docker": (
        "validibot.validations.services.runners.docker.DockerValidatorRunner"
    ),
    "google_cloud_run": (
        "validibot.validations.services.runners.google_cloud_run"
        ".GoogleCloudRunValidatorRunner"
    ),
    "aws_batch": (
        "validibot.validations.services.runners.aws_batch.AWSBatchValidatorRunner"
    ),
}


@lru_cache(maxsize=1)
def get_validator_runner() -> ValidatorRunner:
    """
    Get the configured validator runner backend.

    The runner is selected based on the VALIDATOR_RUNNER setting:
    - "docker" (default): Local Docker socket
    - "google_cloud_run": Google Cloud Run Jobs
    - "aws_batch": AWS Batch (stub)
    - Full class path: Custom runner class

    Configuration:
        VALIDATOR_RUNNER: Runner type or class path
        VALIDATOR_RUNNER_OPTIONS: Dict of options passed to runner constructor

    Returns:
        Configured ValidatorRunner instance (cached singleton)

    Example settings:
        # Self-hosted with Docker (default)
        VALIDATOR_RUNNER = "docker"
        VALIDATOR_RUNNER_OPTIONS = {}

        # Google Cloud Run Jobs
        VALIDATOR_RUNNER = "google_cloud_run"
        VALIDATOR_RUNNER_OPTIONS = {
            "project_id": "my-project",
            "region": "australia-southeast1",
        }

        # AWS Batch
        VALIDATOR_RUNNER = "aws_batch"
        VALIDATOR_RUNNER_OPTIONS = {
            "job_queue": "validibot-validators",
            "region": "ap-southeast-2",
        }
    """
    runner_setting = getattr(settings, "VALIDATOR_RUNNER", "docker")
    options = getattr(settings, "VALIDATOR_RUNNER_OPTIONS", {})

    # Resolve alias to full class path
    runner_class_path = RUNNER_ALIASES.get(runner_setting, runner_setting)

    logger.info(
        "Initializing validator runner: %s",
        runner_class_path,
    )

    # Import and instantiate runner class
    try:
        runner_class = import_string(runner_class_path)
    except ImportError as e:
        msg = f"Could not import validator runner '{runner_class_path}': {e}"
        raise ImportError(msg) from e

    # Create instance with options
    try:
        instance = runner_class(**options)
    except Exception as e:
        msg = f"Could not instantiate validator runner '{runner_class_path}': {e}"
        raise RuntimeError(msg) from e

    return instance


def clear_runner_cache() -> None:
    """
    Clear the cached runner instance.

    Useful for testing or when settings change at runtime.
    """
    get_validator_runner.cache_clear()
