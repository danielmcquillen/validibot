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
    result = runner.run(
        container_image="validibot/validator-energyplus:latest",
        input_uri="file:///app/data/runs/run-123/input.json",
        output_uri="file:///app/data/runs/run-123/output.json",
    )
    if result.succeeded:
        # Process output envelope at result.output_uri
        ...
"""

from validibot.validations.services.runners.base import ExecutionResult
from validibot.validations.services.runners.registry import clear_runner_cache
from validibot.validations.services.runners.registry import get_validator_runner

__all__ = [
    "ExecutionResult",
    "clear_runner_cache",
    "get_validator_runner",
]
