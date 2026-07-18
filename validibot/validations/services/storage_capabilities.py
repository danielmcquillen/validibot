"""Describe the effective validator-runtime storage security boundary.

The immutable-I/O ADR requires deployment health output to distinguish
integrity from confidentiality. A generation-pinned GCS object can be verified
exactly while the Cloud Run job's shared service account can still read a
broader prefix than one execution attempt. Conversely, local Docker gives a
validator only two attempt-specific mounts and therefore provides both exact
byte identity and attempt-scoped authority.

This module deliberately inspects the paired data-storage and validator-runner
settings instead of testing whether generic credentials merely exist. Unknown,
incomplete, and not-yet-implemented combinations fail closed as unsupported.
It performs no network calls, writes no probe objects, and exposes no credential
details, so doctor and support bundles can use it safely.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from validibot.validations.constants import RuntimeStorageIsolation
from validibot.validations.constants import StorageCapabilityMode

_DATA_STORAGE_NAMES = {
    "local": "local",
    "validibot.core.storage.local.localdatastorage": "local",
    "gcs": "gcs",
    "validibot.core.storage.gcs.gcsdatastorage": "gcs",
    "s3": "s3",
    "validibot.core.storage.s3.s3datastorage": "s3",
}

_VALIDATOR_RUNNER_NAMES = {
    "docker": "docker",
    "validibot.validations.services.runners.docker.dockervalidatorrunner": "docker",
    "google_cloud_run": "google_cloud_run",
    (
        "validibot.validations.services.runners.google_cloud_run."
        "googlecloudrunvalidatorrunner"
    ): "google_cloud_run",
    "aws_batch": "aws_batch",
    (
        "validibot.validations.services.runners.aws_batch.awsbatchvalidatorrunner"
    ): "aws_batch",
}


@dataclass(frozen=True)
class StorageCapabilityReport:
    """Machine-readable account of validator storage integrity and isolation.

    Attributes:
        mode: Effective runtime storage mechanism.
        isolation: Confidentiality boundary of the runtime authority.
        data_storage_backend: Configured validation-data backend.
        validator_runner: Configured external-validator runner.
        integrity_enforced: Whether exact size/SHA-256/version verification is
            enforced by the supported execution path.
        create_only_writes: Whether attempt inputs and outputs are published
            without replacing an existing logical identity.
        immutable_reads: Whether the execution binds reads to an immutable
            local identity or object-store version.
        attempt_scoped_authority: Whether a compromised validator runtime is
            prevented from reading or writing another attempt's storage.
        summary: Short operator-facing description of the effective mode.
        limitations: Honest qualifications that must accompany the mode.
        operator_action: Remediation for unsupported or reduced-isolation
            configurations.
    """

    mode: StorageCapabilityMode
    isolation: RuntimeStorageIsolation
    data_storage_backend: str
    validator_runner: str
    integrity_enforced: bool
    create_only_writes: bool
    immutable_reads: bool
    attempt_scoped_authority: bool
    summary: str
    limitations: tuple[str, ...] = ()
    operator_action: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the additive ``validibot.doctor.v1`` JSON projection."""
        return {
            "mode": self.mode.value,
            "isolation": self.isolation.value,
            "data_storage_backend": self.data_storage_backend,
            "validator_runner": self.validator_runner,
            "integrity_enforced": self.integrity_enforced,
            "create_only_writes": self.create_only_writes,
            "immutable_reads": self.immutable_reads,
            "attempt_scoped_authority": self.attempt_scoped_authority,
            "summary": self.summary,
            "limitations": list(self.limitations),
            "operator_action": self.operator_action,
        }


def get_storage_capability_report() -> StorageCapabilityReport:
    """Return the conservative capability report for the running deployment.

    Only the two execution paths whose contracts are currently implemented are
    reported as supported:

    - local data storage through the Docker runner's per-attempt mounts;
    - GCS through Cloud Run with generation-pinned reads and generation-zero
      writes, but a shared runtime service account.

    S3, S3-compatible stores, brokered storage, and custom combinations remain
    unsupported until their live adapter semantics are implemented and tested.
    Merely selecting an S3-looking backend must never be presented as proof of
    conditional-write or versioning support.
    """
    storage_backend = _normalized_setting_name(
        "DATA_STORAGE_BACKEND",
        default="local",
        known_names=_DATA_STORAGE_NAMES,
    )
    validator_runner = _normalized_setting_name(
        "VALIDATOR_RUNNER",
        default="docker",
        known_names=_VALIDATOR_RUNNER_NAMES,
    )

    if storage_backend == "local" and validator_runner == "docker":
        return StorageCapabilityReport(
            mode=StorageCapabilityMode.LOCAL_ATTEMPT_MOUNT,
            isolation=RuntimeStorageIsolation.ATTEMPT_SCOPED,
            data_storage_backend=storage_backend,
            validator_runner=validator_runner,
            integrity_enforced=True,
            create_only_writes=True,
            immutable_reads=True,
            attempt_scoped_authority=True,
            summary=(
                "Validator input is mounted read-only and output read-write for "
                "one execution attempt; exact bytes are verified by size and "
                "SHA-256."
            ),
        )

    if storage_backend == "gcs" and validator_runner == "google_cloud_run":
        limitation = (
            "Cloud Run validator jobs use their configured shared runtime "
            "service account. GCS generations and SHA-256 protect integrity, "
            "but that identity may still read other attempts allowed by IAM."
        )
        return StorageCapabilityReport(
            mode=StorageCapabilityMode.GCS_GENERATION,
            isolation=RuntimeStorageIsolation.REDUCED_SHARED_RUNTIME_IDENTITY,
            data_storage_backend=storage_backend,
            validator_runner=validator_runner,
            integrity_enforced=True,
            create_only_writes=True,
            immutable_reads=True,
            attempt_scoped_authority=False,
            summary=(
                "GCS inputs are generation-pinned and outputs use create-only "
                "generation preconditions."
            ),
            limitations=(limitation,),
            operator_action=(
                "Use attempt-scoped runtime credentials or a server-mediated "
                "broker when that capability is available."
            ),
        )

    if storage_backend == "s3":
        return _unsupported_report(
            storage_backend=storage_backend,
            validator_runner=validator_runner,
            summary=(
                "S3 storage is configured, but conditional writes, immutable "
                "version reads, and scoped runtime authority have not been "
                "capability-tested by this implementation."
            ),
            operator_action=(
                "Do not run external validators on this storage path yet. "
                "Implement and probe S3 conditional/version semantics or route "
                "validator I/O through a server-mediated broker."
            ),
        )

    return _unsupported_report(
        storage_backend=storage_backend,
        validator_runner=validator_runner,
        summary=(
            "The configured data-storage and validator-runner combination has "
            "no verified runtime storage capability contract."
        ),
        operator_action=(
            "Use local + docker or gcs + google_cloud_run. Custom and brokered "
            "paths must publish tested capability semantics before use."
        ),
    )


def _normalized_setting_name(
    name: str,
    *,
    default: str,
    known_names: dict[str, str],
) -> str:
    """Return the canonical alias for a built-in setting class path.

    Registries accept both short aliases and full dotted class paths. Doctor
    must describe those equivalent built-in configurations identically while
    retaining an unknown custom path verbatim so the operator can diagnose it.
    """
    value = getattr(settings, name, default)
    raw_name = str(value or default).strip()
    return known_names.get(raw_name.lower(), raw_name)


def _unsupported_report(
    *,
    storage_backend: str,
    validator_runner: str,
    summary: str,
    operator_action: str,
) -> StorageCapabilityReport:
    """Build the fail-closed report shared by unsupported combinations."""
    return StorageCapabilityReport(
        mode=StorageCapabilityMode.UNSUPPORTED,
        isolation=RuntimeStorageIsolation.UNSUPPORTED,
        data_storage_backend=storage_backend,
        validator_runner=validator_runner,
        integrity_enforced=False,
        create_only_writes=False,
        immutable_reads=False,
        attempt_scoped_authority=False,
        summary=summary,
        limitations=(
            "No integrity or cross-attempt confidentiality claim is made for "
            "this configuration.",
        ),
        operator_action=operator_action,
    )
