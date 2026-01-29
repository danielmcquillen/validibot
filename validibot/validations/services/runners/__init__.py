"""
Validator runner abstraction for executing container-based validators.

This module provides a pluggable runner system for executing advanced validators
(EnergyPlus, FMI, custom containers) across different deployment targets:

- DockerValidatorRunner: Local Docker socket (self-hosted default)
- GoogleCloudRunValidatorRunner: Google Cloud Run Jobs (GCP hosted)
- AWSBatchValidatorRunner: AWS Batch (future AWS support)

Usage:
    from validibot.validations.services.runners import get_validator_runner

    runner = get_validator_runner()
    execution_id = runner.run(
        container_image="validibot/validator-energyplus:latest",
        input_uri="file:///app/data/runs/run-123/input.json",
    )
"""

from validibot.validations.services.runners.registry import clear_runner_cache
from validibot.validations.services.runners.registry import get_validator_runner

__all__ = [
    "clear_runner_cache",
    "get_validator_runner",
]
