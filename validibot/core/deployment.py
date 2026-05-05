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
            "Valid values: test, local_docker_compose, self_hosted, gcp, aws. "
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


def get_validibot_runtime_version() -> str:
    """Return the app version string for operator compatibility checks.

    Deployment stamps ``VALIDIBOT_VERSION`` explicitly from the latest
    reachable Validibot release tag. If that setting is absent, fall back
    to the installed package version. Validator-backend metadata is
    intentionally ignored: backup/restore compatibility is about the
    Django app schema and code, not the external validator image version.
    """
    explicit = (getattr(settings, "VALIDIBOT_VERSION", "") or "").strip()
    if explicit:
        return explicit

    try:
        from validibot import __version__
    except Exception:
        return "unknown"

    return (__version__ or "").strip() or "unknown"
