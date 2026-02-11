"""
Validator runner abstraction for executing container-based validators.

This module provides a pluggable runner system for executing advanced validators
(EnergyPlus, FMI, custom containers) across different deployment targets:

- DockerValidatorRunner: Local Docker socket (Docker Compose default)
- GoogleCloudRunValidatorRunner: Google Cloud Run Jobs (GCP hosted)
- AWSBatchValidatorRunner: AWS Batch (future AWS support)

Usage:
    from validibot.validations.services.runners import get_validator_runner

    runner = get_validator_runner()
    result = runner.run(
        container_image="validibot/validator-energyplus:latest",
        input_uri="file:///app/data/runs/run-123/input.json",
        output_uri="file:///app/data/runs/run-123/output.json",
        run_id="run-123",
        validator_slug="energyplus",
    )
    if result.succeeded:
        # Process output envelope at result.output_uri
        ...

Container Cleanup (Ryuk Pattern):
    from validibot.validations.services.runners import cleanup_orphaned_containers

    # Remove containers that exceeded timeout + grace period
    removed, failed = cleanup_orphaned_containers(grace_period_seconds=300)
"""

from validibot.validations.services.runners.base import ExecutionResult
from validibot.validations.services.runners.docker import cleanup_all_managed_containers
from validibot.validations.services.runners.docker import cleanup_orphaned_containers
from validibot.validations.services.runners.registry import clear_runner_cache
from validibot.validations.services.runners.registry import get_validator_runner

__all__ = [
    "ExecutionResult",
    "cleanup_all_managed_containers",
    "cleanup_orphaned_containers",
    "clear_runner_cache",
    "get_validator_runner",
]
