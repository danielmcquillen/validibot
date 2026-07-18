"""Canonical storage paths for one validator execution attempt.

Advanced validator inputs and outputs must never share a mutable run-level
location.  This module gives local Docker workspaces and object-storage
launchers one path shape so retries cannot accidentally reuse another
attempt's files.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import urlparse

ATTEMPTS_SUBDIR = "attempts"


def attempt_bundle_relpath(
    *,
    org_id: str,
    run_id: str,
    attempt_id: str,
) -> PurePosixPath:
    """Return ``runs/<org>/<run>/attempts/<attempt>`` after safe validation.

    The identifiers normally come from UUID-backed database fields.  The
    explicit leaf-name check keeps this helper safe in tests, data migrations,
    and any future caller that supplies a different identifier type.

    Args:
        org_id: Organization that owns the validation run.
        run_id: Validation run identifier.
        attempt_id: Execution-attempt identifier.

    Returns:
        A provider-neutral POSIX relative path suitable for filesystem and
        object-storage prefixes.

    Raises:
        ValueError: If any identifier is empty or contains a path component.
    """
    components = {
        "org_id": str(org_id),
        "run_id": str(run_id),
        "attempt_id": str(attempt_id),
    }
    for label, value in components.items():
        _validate_leaf_component(value, label=label)

    return PurePosixPath(
        "runs",
        components["org_id"],
        components["run_id"],
        ATTEMPTS_SUBDIR,
        components["attempt_id"],
    )


def validate_attempt_gcs_uri(
    uri: str,
    *,
    expected_bucket: str,
    org_id: str,
    run_id: str,
    attempt_id: str,
) -> None:
    """Require a GCS object URI to stay inside one execution-attempt prefix.

    Validator containers control callback and output-artifact URI strings. This
    helper rebuilds the only permitted object prefix from trusted database
    identities, preventing one attempt from referring to another run, attempt,
    organization, or bucket.

    Args:
        uri: Candidate ``gs://`` object URI.
        expected_bucket: Deployment-owned validation bucket.
        org_id: Trusted organization identifier.
        run_id: Trusted validation-run identifier.
        attempt_id: Trusted execution-attempt identifier.

    Raises:
        ValueError: If the URI is malformed or escapes the expected prefix.
    """
    parsed = urlparse(uri)
    blob_path = parsed.path.removeprefix("/")
    expected_relpath = attempt_bundle_relpath(
        org_id=org_id,
        run_id=run_id,
        attempt_id=attempt_id,
    )
    expected_prefix = f"{expected_relpath.as_posix()}/"
    if (
        parsed.scheme != "gs"
        or parsed.netloc != expected_bucket
        or not blob_path
        or not blob_path.startswith(expected_prefix)
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        msg = "GCS URI is outside the permitted execution-attempt prefix"
        raise ValueError(msg)


def _validate_leaf_component(value: str, *, label: str) -> None:
    """Reject values that could escape or reshape the attempt prefix."""
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or PurePosixPath(value).is_absolute()
    ):
        msg = f"Unsafe {label} for execution-attempt path: {value!r}"
        raise ValueError(msg)


__all__ = [
    "ATTEMPTS_SUBDIR",
    "attempt_bundle_relpath",
    "validate_attempt_gcs_uri",
]
