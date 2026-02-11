"""
Deployment target utilities.

This module provides helpers for determining and validating the deployment target.
The deployment target controls platform-specific behavior throughout the application.
"""

from __future__ import annotations

from django.conf import settings

from validibot.core.constants import DeploymentTarget


def get_deployment_target() -> DeploymentTarget:
    """
    Get and validate the DEPLOYMENT_TARGET setting.

    The deployment target identifies the deployment environment and controls:
    - Task dispatcher selection (how validation tasks are enqueued)
    - Execution backend selection (how validator containers are run)
    - Storage backend selection (where files are stored)

    Returns:
        DeploymentTarget enum value.

    Raises:
        ValueError: If DEPLOYMENT_TARGET is not set or invalid.

    Example:
        ```python
        from validibot.core.deployment import get_deployment_target
        from validibot.core.constants import DeploymentTarget

        target = get_deployment_target()
        if target == DeploymentTarget.GCP:
            # Use GCP-specific configuration
            ...
        ```
    """
    deployment_target = getattr(settings, "DEPLOYMENT_TARGET", None)

    if not deployment_target:
        msg = (
            "DEPLOYMENT_TARGET setting is required. "
            "Valid values: test, local_docker_compose, docker_compose, gcp, aws. "
            "Set this in your settings file or DEPLOYMENT_TARGET environment variable."
        )
        raise ValueError(msg)

    try:
        return DeploymentTarget(deployment_target)
    except ValueError:
        valid_targets = ", ".join(t.value for t in DeploymentTarget)
        msg = (
            f"Invalid DEPLOYMENT_TARGET: {deployment_target}. "
            f"Valid values: {valid_targets}"
        )
        raise ValueError(msg) from None
