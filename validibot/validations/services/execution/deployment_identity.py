"""Canonical hashes for immutable validator deployment configuration."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot.validations.models import ValidatorExecutionDeployment


def _canonical_sha256(value: dict[str, object]) -> str:
    """Return lowercase SHA-256 for stable, whitespace-free JSON."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def provider_spec_sha256(deployment: ValidatorExecutionDeployment) -> str:
    """Hash immutable provider identity without mutable warm-capacity values."""
    return _canonical_sha256(
        {
            "provider_type": deployment.provider_type,
            "deployment_kind": deployment.deployment_kind,
            "deployment_revision": deployment.deployment_revision,
            "provider_configuration": deployment.provider_configuration,
            "provider_resource_name": deployment.provider_resource_name,
            "route": deployment.route,
            "authentication_audience": deployment.authentication_audience,
            "backend_image_ref": deployment.backend_image_ref,
            "backend_image_digest": deployment.backend_image_digest,
            "expected_runtime_identity": deployment.expected_runtime_identity,
        }
    )


def execution_config_sha256(deployment: ValidatorExecutionDeployment) -> str:
    """Hash the immutable runtime contract and execution bounds."""
    return _canonical_sha256(
        {
            "declared_capabilities": deployment.declared_capabilities,
            "maximum_execution_seconds": deployment.maximum_execution_seconds,
            "request_timeout_seconds": deployment.request_timeout_seconds,
            "dispatch_timeout_seconds": deployment.dispatch_timeout_seconds,
            "concurrency": deployment.concurrency,
        }
    )


def set_deployment_config_digests(
    deployment: ValidatorExecutionDeployment,
) -> ValidatorExecutionDeployment:
    """Set both canonical hashes before a deployment first reaches READY."""
    deployment.provider_spec_sha256 = provider_spec_sha256(deployment)
    deployment.execution_config_sha256 = execution_config_sha256(deployment)
    return deployment
