"""Run live provider checks for one attempt-scoped GCS capability.

Deployment diagnostics must not infer isolation from configuration alone. This
module creates two temporary sibling attempt prefixes, issues the same Google
Credential Access Boundary token used by Cloud Run validators, and exercises
the provider with that explicit credential. The token must read and create
inside its attempt while being unable to read or create in the sibling attempt,
replace an existing object, or delete an object.

The trusted Django identity owns setup and cleanup. Every object is created
below a random ``runs/validibot-capability-probes/`` prefix, and cleanup lists
only that unique prefix. Bearer tokens are never included in results or logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import Any
from uuid import uuid4

from google.api_core.exceptions import Forbidden
from google.cloud import storage  # type: ignore[attr-defined]
from google.oauth2.credentials import Credentials

from validibot.validations.services.cloud_run.gcs_runtime_capabilities import (
    issue_attempt_gcs_runtime_capability,
)

GCS_CAPABILITY_PROBE_SCHEMA_VERSION = "validibot.gcs-capability-probe.v1"
_PROBE_ROOT = "runs/validibot-capability-probes"
_CONTROL_BYTES = b"validibot-gcs-capability-probe\n"


class GCSCapabilityProbeError(RuntimeError):
    """Raised when provider probe setup cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class GCSCapabilityProbeCheck:
    """One provider operation and its security verdict."""

    name: str
    passed: bool
    expected: str
    observed: str

    def as_dict(self) -> dict[str, object]:
        """Return a stable, credential-free machine projection."""
        return {
            "name": self.name,
            "passed": self.passed,
            "expected": self.expected,
            "observed": self.observed,
        }


@dataclass(frozen=True, slots=True)
class GCSCapabilityProbeReport:
    """Aggregate verdict for the live downscoped-token provider checks."""

    checks: tuple[GCSCapabilityProbeCheck, ...]

    @property
    def passed(self) -> bool:
        """Return true only when every provider and cleanup check passed."""
        return bool(self.checks) and all(check.passed for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        """Return the versioned operator/automation result."""
        return {
            "schema_version": GCS_CAPABILITY_PROBE_SCHEMA_VERSION,
            "passed": self.passed,
            "checks": [check.as_dict() for check in self.checks],
        }


def probe_attempt_gcs_runtime_capability(
    *,
    bucket_name: str,
    project_id: str,
) -> GCSCapabilityProbeReport:
    """Exercise the real GCS boundary with one freshly downscoped token.

    The caller must be the trusted Django identity: it creates the two control
    objects, exchanges its ADC for the bounded credential, and removes the
    unique probe prefix afterward. A failed or partial cleanup is itself a
    failed check so operators never mistake a residue-producing run for proof.
    """
    if not bucket_name or "/" in bucket_name or bucket_name.startswith("gs://"):
        raise GCSCapabilityProbeError("A bare GCS bucket name is required")
    if not project_id:
        raise GCSCapabilityProbeError("A GCP project ID is required")

    probe_id = str(uuid4())
    probe_root = f"{_PROBE_ROOT}/{probe_id}/"
    allowed_prefix = f"{probe_root}attempts/allowed/"
    denied_prefix = f"{probe_root}attempts/denied/"
    allowed_input_name = f"{allowed_prefix}input.txt"
    allowed_output_name = f"{allowed_prefix}output.txt"
    denied_input_name = f"{denied_prefix}input.txt"
    denied_output_name = f"{denied_prefix}output.txt"

    trusted_client = storage.Client(project=project_id)
    trusted_bucket = trusted_client.bucket(bucket_name)
    checks: list[GCSCapabilityProbeCheck] = []
    setup_error: Exception | None = None

    try:
        allowed_generation = _create_control_object(
            trusted_bucket.blob(allowed_input_name)
        )
        _create_control_object(trusted_bucket.blob(denied_input_name))

        capability = issue_attempt_gcs_runtime_capability(
            execution_bundle_uri=f"gs://{bucket_name}/{allowed_prefix}",
            project_id=project_id,
            refresh_url="https://invalid.example/validibot-capability-probe",
        )
        explicit_credentials = Credentials(
            token=capability.access_token,
            expiry=capability.expires_at.astimezone(UTC).replace(tzinfo=None),
        )
        capability_client = storage.Client(
            project=project_id,
            credentials=explicit_credentials,
        )
        capability_bucket = capability_client.bucket(bucket_name)

        checks.append(
            _expect_success(
                name="allowed_generation_read",
                operation=lambda: capability_bucket.blob(
                    allowed_input_name,
                    generation=allowed_generation,
                ).download_as_bytes(if_generation_match=allowed_generation),
            )
        )
        checks.append(
            _expect_success(
                name="allowed_create",
                operation=lambda: capability_bucket.blob(
                    allowed_output_name
                ).upload_from_string(_CONTROL_BYTES, if_generation_match=0),
            )
        )
        checks.append(
            _expect_forbidden(
                name="cross_attempt_read",
                operation=lambda: capability_bucket.blob(
                    denied_input_name
                ).download_as_bytes(),
            )
        )
        checks.append(
            _expect_forbidden(
                name="cross_attempt_create",
                operation=lambda: capability_bucket.blob(
                    denied_output_name
                ).upload_from_string(_CONTROL_BYTES, if_generation_match=0),
            )
        )
        checks.append(
            _expect_forbidden(
                name="existing_object_overwrite",
                operation=lambda: capability_bucket.blob(
                    allowed_input_name
                ).upload_from_string(b"replacement-must-be-denied\n"),
            )
        )
        checks.append(
            _expect_forbidden(
                name="object_delete",
                operation=lambda: capability_bucket.blob(
                    allowed_input_name,
                    generation=allowed_generation,
                ).delete(if_generation_match=allowed_generation),
            )
        )
    except Exception as exc:  # Setup/issuance is reported after cleanup.
        setup_error = exc
    finally:
        checks.append(
            _cleanup_probe_prefix(
                bucket=trusted_bucket,
                probe_root=probe_root,
            )
        )

    if setup_error is not None:
        error_name = type(setup_error).__name__
        cleanup_state = checks[-1].observed if checks else "not-run"
        raise GCSCapabilityProbeError(
            f"GCS capability probe setup failed ({error_name}); cleanup={cleanup_state}"
        ) from setup_error
    return GCSCapabilityProbeReport(checks=tuple(checks))


def _create_control_object(blob) -> int:
    """Publish one unique trusted control object and return its generation."""
    blob.upload_from_string(_CONTROL_BYTES, if_generation_match=0)
    return _required_blob_generation(blob, purpose="control publication")


def _required_blob_generation(blob, *, purpose: str) -> int:
    """Return a blob generation or fail before an unfenced operation."""
    if blob.generation is None:
        blob.reload()
    if blob.generation is None:
        raise GCSCapabilityProbeError(f"GCS did not return a generation for {purpose}")
    return int(blob.generation)


def _expect_success(*, name: str, operation) -> GCSCapabilityProbeCheck:
    """Run an operation that the attempt token must be able to perform."""
    try:
        operation()
    except Exception as exc:
        return GCSCapabilityProbeCheck(
            name=name,
            passed=False,
            expected="allowed",
            observed=_safe_exception_observation(exc),
        )
    return GCSCapabilityProbeCheck(
        name=name,
        passed=True,
        expected="allowed",
        observed="allowed",
    )


def _expect_forbidden(*, name: str, operation) -> GCSCapabilityProbeCheck:
    """Run an operation that the attempt token must be denied by GCS."""
    try:
        operation()
    except Forbidden:
        return GCSCapabilityProbeCheck(
            name=name,
            passed=True,
            expected="forbidden",
            observed="forbidden",
        )
    except Exception as exc:
        return GCSCapabilityProbeCheck(
            name=name,
            passed=False,
            expected="forbidden",
            observed=_safe_exception_observation(exc),
        )
    return GCSCapabilityProbeCheck(
        name=name,
        passed=False,
        expected="forbidden",
        observed="allowed",
    )


def _cleanup_probe_prefix(*, bucket, probe_root: str) -> GCSCapabilityProbeCheck:
    """Delete only the random probe prefix with trusted generation fencing."""
    try:
        for blob in bucket.list_blobs(prefix=probe_root):
            generation = _required_blob_generation(blob, purpose="probe cleanup")
            blob.delete(if_generation_match=generation)
    except Exception as exc:
        return GCSCapabilityProbeCheck(
            name="trusted_cleanup",
            passed=False,
            expected="complete",
            observed=_safe_exception_observation(exc),
        )
    return GCSCapabilityProbeCheck(
        name="trusted_cleanup",
        passed=True,
        expected="complete",
        observed="complete",
    )


def _safe_exception_observation(exc: Exception) -> str:
    """Describe a provider failure without copying messages or credentials."""
    code: Any = getattr(exc, "code", None)
    if callable(code):
        code = code()
    suffix = f":{code}" if isinstance(code, int | str) else ""
    return f"{type(exc).__name__}{suffix}"
