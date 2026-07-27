"""Reference-aware retention planning for managed validator backend images."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta

from django.utils import timezone

from validibot.validations.constants import EXECUTION_ATTEMPT_TERMINAL_STATES
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentReadiness
from validibot.validations.constants import ExecutionProviderType
from validibot.validations.models import ExecutionAttempt
from validibot.validations.models import ValidatorExecutionDeployment

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_GCP_RUNNER_TYPES = frozenset(
    {
        "CloudRunJobsExecutionBackend",
        "CloudRunServiceExecutionBackend",
        "cloud_run_job",
        "google_cloud_run",
    }
)


@dataclass(frozen=True)
class BackendImageProtection:
    """One immutable digest and the database references that protect it."""

    digest: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class BackendImageProtectionPlan:
    """A secret-free snapshot consumed by the GCP cleanup operator command."""

    grace_days: int
    generated_at: datetime
    protected: tuple[BackendImageProtection, ...]
    blockers: tuple[str, ...]


def normalize_backend_digest(value: object) -> str:
    """Return the lowercase sha256 digest embedded in a stored image value."""
    match = _DIGEST_RE.search(str(value or ""))
    return match.group(0) if match is not None else ""


def _attempt_is_managed_gcp(attempt: ExecutionAttempt) -> bool:
    """Return whether an active attempt can refer to a managed GCP image."""
    if (
        attempt.deployment is not None
        and attempt.deployment.provider_type == ExecutionProviderType.GCP
    ):
        return True
    if attempt.runner_type in _GCP_RUNNER_TYPES:
        return True
    return "/locations/" in attempt.provider_resource_name and (
        "/jobs/" in attempt.provider_resource_name
        or "/services/" in attempt.provider_resource_name
    )


def build_backend_image_protection_plan(
    *,
    grace_days: int = 7,
    now=None,
) -> BackendImageProtectionPlan:
    """Protect every database reference required by backend image cleanup.

    Non-retired Job and Service deployments remain protected even while
    inactive. Retired Services retain a short rollback grace period. Every
    nonterminal managed attempt protects its immutable snapshot independently
    of deployment state.
    """
    if grace_days < 0:
        raise ValueError("grace_days must be zero or greater.")

    checked_at = now or timezone.now()
    grace_cutoff = checked_at - timedelta(days=grace_days)
    reasons_by_digest: dict[str, set[str]] = defaultdict(set)
    blockers: list[str] = []

    managed_deployments = ValidatorExecutionDeployment.objects.filter(
        provider_type=ExecutionProviderType.GCP,
        deployment_kind__in=(
            ExecutionDeploymentKind.CLOUD_RUN_JOB,
            ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        ),
    ).order_by("pk")
    for deployment in managed_deployments:
        digest = normalize_backend_digest(deployment.backend_image_digest)
        deployment_label = (
            "Job"
            if deployment.deployment_kind == ExecutionDeploymentKind.CLOUD_RUN_JOB
            else "Service"
        )
        if deployment.readiness_state != ExecutionDeploymentReadiness.RETIRED:
            if digest:
                reasons_by_digest[digest].add(
                    f"non-retired {deployment_label} deployment {deployment.pk}"
                )
            else:
                blockers.append(
                    f"non-retired {deployment_label} deployment "
                    f"{deployment.pk} has no valid digest"
                )
        elif (
            deployment.deployment_kind == ExecutionDeploymentKind.CLOUD_RUN_SERVICE
            and deployment.modified >= grace_cutoff
        ):
            if digest:
                reasons_by_digest[digest].add(
                    f"Service deployment {deployment.pk} retirement grace"
                )
            else:
                blockers.append(
                    f"recently retired Service deployment {deployment.pk} "
                    "has no valid digest"
                )

    active_attempts = (
        ExecutionAttempt.objects.exclude(state__in=EXECUTION_ATTEMPT_TERMINAL_STATES)
        .select_related("deployment")
        .order_by("pk")
    )
    for attempt in active_attempts:
        if not _attempt_is_managed_gcp(attempt):
            continue
        digest = normalize_backend_digest(
            attempt.backend_image_digest
            or (
                attempt.deployment.backend_image_digest
                if attempt.deployment is not None
                else ""
            )
            or attempt.deployment_snapshot.get("backend_image_digest", "")
        )
        if not digest:
            blockers.append(
                f"nonterminal managed attempt {attempt.pk} has no valid digest"
            )
            continue
        reasons_by_digest[digest].add(f"nonterminal attempt {attempt.pk}")

    protected = tuple(
        BackendImageProtection(
            digest=digest,
            reasons=tuple(sorted(reasons)),
        )
        for digest, reasons in sorted(reasons_by_digest.items())
    )
    return BackendImageProtectionPlan(
        grace_days=grace_days,
        generated_at=checked_at,
        protected=protected,
        blockers=tuple(sorted(blockers)),
    )


def validator_job_update_blockers(*, job_name: str) -> tuple[str, ...]:
    """Return attempts blocking a legacy fixed-name Job update.

    This is a transitional guard while stable Jobs are still updated in place.
    Release-specific Jobs are created under new provider names and do not use
    this update path.
    """
    if not job_name:
        raise ValueError("job_name is required.")

    blockers: list[str] = []
    active_attempts = (
        ExecutionAttempt.objects.exclude(state__in=EXECUTION_ATTEMPT_TERMINAL_STATES)
        .select_related("deployment")
        .order_by("pk")
    )
    for attempt in active_attempts:
        deployment = attempt.deployment
        deployment_job_name = ""
        if (
            deployment is not None
            and deployment.provider_type == ExecutionProviderType.GCP
            and deployment.deployment_kind == ExecutionDeploymentKind.CLOUD_RUN_JOB
        ):
            deployment_job_name = str(
                deployment.provider_configuration.get("job_name", "")
            )
        resource_names = {
            attempt.provider_resource_name,
            deployment.provider_resource_name if deployment is not None else "",
        }
        resource_matches = any(
            resource == job_name or resource.endswith(f"/jobs/{job_name}")
            for resource in resource_names
            if resource
        )
        if deployment_job_name == job_name or resource_matches:
            blockers.append(
                f"attempt {attempt.pk} is {attempt.state} on fixed Job {job_name}"
            )
    return tuple(blockers)


__all__ = [
    "BackendImageProtection",
    "BackendImageProtectionPlan",
    "build_backend_image_protection_plan",
    "normalize_backend_digest",
    "validator_job_update_blockers",
]
