"""Tests for truthful validator-runtime storage capability diagnostics.

The doctor must report the effective runner/storage pair, not infer safety from
the presence of credentials or a provider name. These tests pin the two
implemented paths and make unsupported S3, broker, custom, and mismatched
configurations fail closed. This matters because immutable-byte verification
protects integrity but does not itself stop a compromised shared runtime
identity from reading another execution attempt.
"""

from django.test import override_settings

from validibot.validations.constants import RuntimeStorageIsolation
from validibot.validations.constants import StorageCapabilityMode
from validibot.validations.services.storage_capabilities import (
    get_storage_capability_report,
)


@override_settings(DATA_STORAGE_BACKEND="local", VALIDATOR_RUNNER="docker")
def test_local_docker_capability_is_attempt_scoped():
    """The supported Docker path exposes only one attempt's input/output mounts."""
    report = get_storage_capability_report()

    assert report.mode is StorageCapabilityMode.LOCAL_ATTEMPT_MOUNT
    assert report.isolation is RuntimeStorageIsolation.ATTEMPT_SCOPED
    assert report.integrity_enforced is True
    assert report.create_only_writes is True
    assert report.immutable_reads is True
    assert report.attempt_scoped_authority is True
    assert report.limitations == ()


@override_settings(
    DATA_STORAGE_BACKEND="gcs",
    VALIDATOR_RUNNER="google_cloud_run",
)
def test_gcs_capability_separates_integrity_from_confidentiality():
    """Generation checks stay true while shared IAM is labelled reduced isolation."""
    report = get_storage_capability_report()

    assert report.mode is StorageCapabilityMode.GCS_GENERATION
    assert report.isolation is RuntimeStorageIsolation.REDUCED_SHARED_RUNTIME_IDENTITY
    assert report.integrity_enforced is True
    assert report.create_only_writes is True
    assert report.immutable_reads is True
    assert report.attempt_scoped_authority is False
    assert "shared runtime service account" in report.limitations[0]


@override_settings(DATA_STORAGE_BACKEND="s3", VALIDATOR_RUNNER="aws_batch")
def test_s3_capability_is_unsupported_until_adapter_is_probed():
    """An S3 label alone does not prove conditional or version semantics."""
    report = get_storage_capability_report()

    assert report.mode is StorageCapabilityMode.UNSUPPORTED
    assert report.isolation is RuntimeStorageIsolation.UNSUPPORTED
    assert report.integrity_enforced is False
    assert report.create_only_writes is False
    assert report.immutable_reads is False
    assert report.attempt_scoped_authority is False
    assert "capability-tested" in report.summary


@override_settings(DATA_STORAGE_BACKEND="gcs", VALIDATOR_RUNNER="docker")
def test_mismatched_runner_and_storage_fail_closed():
    """Docker cannot claim local mounts when its data backend is GCS."""
    report = get_storage_capability_report()

    assert report.mode is StorageCapabilityMode.UNSUPPORTED
    assert report.data_storage_backend == "gcs"
    assert report.validator_runner == "docker"
    assert "no verified" in report.summary


@override_settings(
    DATA_STORAGE_BACKEND="validibot.core.storage.local.LocalDataStorage",
    VALIDATOR_RUNNER=(
        "validibot.validations.services.runners.docker.DockerValidatorRunner"
    ),
)
def test_builtin_dotted_paths_resolve_to_their_canonical_capability():
    """Registry class paths behave exactly like their short built-in aliases."""
    report = get_storage_capability_report()

    assert report.mode is StorageCapabilityMode.LOCAL_ATTEMPT_MOUNT
    assert report.data_storage_backend == "local"
    assert report.validator_runner == "docker"


@override_settings(
    DATA_STORAGE_BACKEND="acme.storage.BrokeredStorage",
    VALIDATOR_RUNNER="acme.runner.RemoteRunner",
)
def test_custom_or_brokered_path_requires_a_tested_contract():
    """Custom class paths stay unsupported until a capability adapter exists."""
    report = get_storage_capability_report()

    assert report.mode is StorageCapabilityMode.UNSUPPORTED
    assert report.attempt_scoped_authority is False
    assert "brokered" in report.operator_action.lower()


@override_settings(DATA_STORAGE_BACKEND="local", VALIDATOR_RUNNER="docker")
def test_json_projection_contains_no_paths_or_credentials():
    """Support-safe JSON contains capability facts but no secret-bearing values."""
    payload = get_storage_capability_report().as_dict()

    assert payload["mode"] == "local_attempt_mount"
    assert payload["isolation"] == "attempt_scoped"
    assert payload["limitations"] == []
    assert set(payload) == {
        "mode",
        "isolation",
        "data_storage_backend",
        "validator_runner",
        "integrity_enforced",
        "create_only_writes",
        "immutable_reads",
        "attempt_scoped_authority",
        "summary",
        "limitations",
        "operator_action",
    }
