"""Calculate validator release names, status, and cleanup plans locally.

This program is intentionally provider-neutral at its core. The public
``just gcp`` recipes collect JSON from the remote Django management command,
Cloud Run, Artifact Registry, and retained release-record storage. This
program combines those read-only inputs with the sibling backend repository's
``backends.toml``. It never imports application settings and never contacts
GCP by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

INVENTORY_SCHEMA_VERSION = 2
STATE_SCHEMA_VERSION = "validibot.validator-release-state.v1"
STATUS_SCHEMA_VERSION = "validibot.validator-release-status.v1"
ACTIVATION_CHECK_SCHEMA_VERSION = "validibot.validator-activation-check.v1"
CLEANUP_SCHEMA_VERSION = "validibot.validator-release-cleanup.v1"
RELEASE_RECORD_FIELDS = {
    "schema_version",
    "backend",
    "version",
    "source_tag",
    "source_commit",
    "image",
    "image_digest",
    "shared_contract",
    "sbom",
    "build_verification",
}
SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
PROVIDER_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
PROVIDER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
MAX_PROVIDER_NAME_LENGTH = 63
DEFAULT_DRAIN_DAYS = 7


class ReleaseControlError(ValueError):
    """An input cannot produce a safe deterministic operator decision."""


@dataclass(frozen=True, slots=True)
class SemVer:
    """Small SemVer 2.0 precedence value; build metadata is non-ordering."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> SemVer:
        """Parse a complete semantic version or fail with the exact value."""
        match = SEMVER_PATTERN.fullmatch(value)
        if match is None:
            raise ReleaseControlError(f"Invalid semantic version: {value!r}")
        prerelease = tuple((match.group("prerelease") or "").split("."))
        if prerelease == ("",):
            prerelease = ()
        for identifier in prerelease:
            if identifier.isdigit() and len(identifier) > 1 and identifier[0] == "0":
                raise ReleaseControlError(
                    f"Invalid numeric prerelease identifier in {value!r}"
                )
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            prerelease,
        )

    def _compare_prerelease(self, other: SemVer) -> int:
        if not self.prerelease and not other.prerelease:
            return 0
        if not self.prerelease:
            return 1
        if not other.prerelease:
            return -1
        for left, right in zip(self.prerelease, other.prerelease, strict=False):
            if left == right:
                continue
            if left.isdigit() and right.isdigit():
                return -1 if int(left) < int(right) else 1
            if left.isdigit() != right.isdigit():
                return -1 if left.isdigit() else 1
            return -1 if left < right else 1
        return (len(self.prerelease) > len(other.prerelease)) - (
            len(self.prerelease) < len(other.prerelease)
        )

    def compare(self, other: SemVer) -> int:
        """Return -1, 0, or 1 using SemVer precedence."""
        left_core = (self.major, self.minor, self.patch)
        right_core = (other.major, other.minor, other.patch)
        if left_core != right_core:
            return (left_core > right_core) - (left_core < right_core)
        return self._compare_prerelease(other)


@dataclass(frozen=True, slots=True)
class BackendIntent:
    """One release-enabled entry from the only local backend inventory."""

    slug: str
    provider_resource_slug: str
    release_version: str
    image_name: str

    @property
    def source_tag(self) -> str:
        """Return the exact signed Git tag required by this release."""
        return f"{self.slug}-v{self.release_version}"


def default_inventory_path() -> Path:
    """Return the sibling backend inventory path from this public repository."""
    return (
        Path(__file__).resolve().parents[3]
        / "validibot-validator-backends"
        / "backends.toml"
    )


def load_inventory(path: Path) -> tuple[BackendIntent, ...]:
    """Load and validate release intent from ``backends.toml`` schema 2."""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseControlError(
            f"Cannot read backend inventory {path}: {exc}"
        ) from exc
    if document.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        raise ReleaseControlError(
            f"{path} must use schema_version = {INVENTORY_SCHEMA_VERSION}"
        )
    intents: list[BackendIntent] = []
    seen_slugs: set[str] = set()
    seen_provider_slugs: set[str] = set()
    for raw in document.get("backend", []):
        if raw.get("release") is not True:
            continue
        slug = str(raw.get("slug", ""))
        provider_slug = str(raw.get("provider_resource_slug", ""))
        version = str(raw.get("release_version", ""))
        image_name = str(raw.get("image_name", ""))
        if SLUG_PATTERN.fullmatch(slug) is None or slug in seen_slugs:
            raise ReleaseControlError(f"Invalid or duplicate backend slug: {slug!r}")
        if (
            PROVIDER_SLUG_PATTERN.fullmatch(provider_slug) is None
            or provider_slug in seen_provider_slugs
        ):
            raise ReleaseControlError(
                f"Invalid or duplicate provider resource slug: {provider_slug!r}"
            )
        SemVer.parse(version)
        expected_image_suffix = slug.replace("_", "-")
        if not image_name.endswith(expected_image_suffix):
            raise ReleaseControlError(
                f"Backend {slug!r} image_name does not end with "
                f"{expected_image_suffix!r}"
            )
        intent = BackendIntent(slug, provider_slug, version, image_name)
        for kind in ("service", "job"):
            provider_resource_name(intent, kind=kind, stage="prod")
            provider_resource_name(intent, kind=kind, stage="staging")
            provider_resource_name(intent, kind=kind, stage="dev")
        intents.append(intent)
        seen_slugs.add(slug)
        seen_provider_slugs.add(provider_slug)
    if not intents:
        raise ReleaseControlError(f"{path} has no release-enabled backends")
    return tuple(intents)


def _normalized_version(version: str) -> str:
    """Convert a verified SemVer to the lossless provider-name form."""
    SemVer.parse(version)
    without_build, separator, build = version.partition("+")
    core, prerelease_separator, prerelease = without_build.partition("-")
    normalized = core.replace(".", "-").lower()
    if prerelease_separator:
        normalized += f"-pre-{prerelease.replace('.', '-').lower()}"
    if separator:
        normalized += f"-build-{build.replace('.', '-').lower()}"
    if not normalized:
        raise ReleaseControlError(f"Version {version!r} has no provider-safe value")
    return normalized


def provider_resource_name(
    intent: BackendIntent,
    *,
    kind: str,
    stage: str,
) -> str:
    """Build one bounded release-specific Service or Job name."""
    prefixes = {"service": "vb-vs", "job": "vb-vj"}
    if kind not in prefixes:
        raise ReleaseControlError("Provider kind must be 'service' or 'job'")
    if stage not in {"prod", "staging", "dev"}:
        raise ReleaseControlError("Stage must be prod, staging, or dev")
    if stage == "dev":
        name = f"{prefixes[kind]}-{intent.provider_resource_slug}-dev"
    else:
        suffix = "-stg" if stage == "staging" else ""
        name = (
            f"{prefixes[kind]}-{intent.provider_resource_slug}-"
            f"v{_normalized_version(intent.release_version)}{suffix}"
        )
    if (
        len(name) > MAX_PROVIDER_NAME_LENGTH
        or PROVIDER_NAME_PATTERN.fullmatch(name) is None
    ):
        raise ReleaseControlError(
            f"Provider name {name!r} is invalid or exceeds "
            f"{MAX_PROVIDER_NAME_LENGTH} characters; choose a shorter "
            "provider_resource_slug"
        )
    return name


def validate_release_record(
    record: dict[str, Any],
    *,
    intent: BackendIntent,
) -> None:
    """Require the exact ADR release-record shape and selected release identity."""
    if set(record) != RELEASE_RECORD_FIELDS:
        missing = sorted(RELEASE_RECORD_FIELDS - set(record))
        extra = sorted(set(record) - RELEASE_RECORD_FIELDS)
        raise ReleaseControlError(
            f"Release record fields differ; missing={missing}, extra={extra}"
        )
    expected = {
        "schema_version": "validibot.backend-release.v1",
        "backend": intent.slug,
        "version": intent.release_version,
        "source_tag": intent.source_tag,
    }
    for field, value in expected.items():
        if record[field] != value:
            raise ReleaseControlError(
                f"Release record {field} is {record[field]!r}, expected {value!r}"
            )
    if IMAGE_DIGEST_PATTERN.fullmatch(str(record["image_digest"])) is None:
        raise ReleaseControlError("Release record image_digest is not sha256")
    if SOURCE_COMMIT_PATTERN.fullmatch(str(record["source_commit"])) is None:
        raise ReleaseControlError("Release record source_commit is not a Git hash")
    SemVer.parse(str(record["shared_contract"]))
    if not str(record["image"]).endswith(f"/{intent.image_name}"):
        raise ReleaseControlError(
            "Release record image does not name the selected backend repository"
        )
    if "@" in str(record["image"]) or ":" in str(record["image"]).rsplit("/", 1)[-1]:
        raise ReleaseControlError(
            "Release record image must name a repository without a tag or digest"
        )
    for field in ("sbom", "build_verification"):
        if not str(record[field]).strip():
            raise ReleaseControlError(f"Release record {field} must not be empty")
    for field in ("source_commit", "shared_contract", "sbom", "build_verification"):
        if not str(record[field]).strip():
            raise ReleaseControlError(f"Release record {field} is empty")


def release_record_sha256(path: Path, *, intent: BackendIntent) -> str:
    """Validate one downloaded release JSON file and return its exact SHA-256."""
    payload = path.read_bytes()
    try:
        record = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ReleaseControlError(f"Release record is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise ReleaseControlError("Release record root must be a JSON object")
    validate_release_record(record, intent=intent)
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path, *, expected_type: type) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseControlError(f"Cannot read JSON input {path}: {exc}") from exc
    if not isinstance(value, expected_type):
        raise ReleaseControlError(
            f"{path} must contain a JSON {expected_type.__name__}"
        )
    return value


def _database_releases(
    database: dict[str, Any],
    *,
    backend: str,
) -> dict[str, list[dict[str, Any]]]:
    releases: dict[str, list[dict[str, Any]]] = {}
    for row in database.get("deployments", []):
        if row.get("backend") == backend:
            releases.setdefault(str(row.get("version", "")), []).append(row)
    return releases


def _pair_complete(rows: list[dict[str, Any]]) -> bool:
    kinds = {row.get("kind") for row in rows}
    return {"CLOUD_RUN_SERVICE", "CLOUD_RUN_JOB"}.issubset(kinds)


def _active_release(rows_by_version: dict[str, list[dict[str, Any]]]) -> str | None:
    active = {
        version
        for version, rows in rows_by_version.items()
        if any(row.get("routing_role") in {"PRIMARY", "LONG_RUNNING"} for row in rows)
    }
    if len(active) > 1:
        raise ReleaseControlError(
            f"Backend has mixed active release versions: {sorted(active)}"
        )
    return next(iter(active), None)


def _routing_mode(rows: list[dict[str, Any]]) -> str:
    service_roles = {
        row.get("routing_role")
        for row in rows
        if row.get("kind") == "CLOUD_RUN_SERVICE"
    }
    job_roles = {
        row.get("routing_role") for row in rows if row.get("kind") == "CLOUD_RUN_JOB"
    }
    if "PRIMARY" in service_roles and "LONG_RUNNING" in job_roles:
        return "normal"
    if service_roles == {"INACTIVE"} and "PRIMARY" in job_roles:
        return "job-only"
    if service_roles <= {"INACTIVE"} and job_roles <= {"INACTIVE"}:
        return "inactive"
    return "inconsistent"


def _rolled_back_versions(
    database: dict[str, Any],
    *,
    backend: str,
) -> dict[str, dict[str, str | None]]:
    """Return rollback time and recorded repair context for each release."""
    values: dict[str, dict[str, str | None]] = {}
    deployments = database.get("deployments", [])
    deployments_by_id = {
        str(row.get("deployment_id")): row
        for row in deployments
        if row.get("deployment_id")
    }
    for row in deployments:
        if (
            row.get("backend") == backend
            and row.get("deactivation_cause") == "RELEASE_ROLLBACK_FROM"
        ):
            values[str(row.get("version"))] = {
                "at": row.get("deactivated_at"),
                "reason": None,
            }
    for event in database.get("routing_events", []):
        metadata = event.get("metadata") or {}
        changes = event.get("changes") or {}
        cause = changes.get("deactivation_cause")
        if (
            not isinstance(cause, list)
            or not cause
            or cause[-1] != "RELEASE_ROLLBACK_FROM"
        ):
            continue
        deployment = deployments_by_id.get(str(event.get("deployment_id", "")))
        if deployment is None or deployment.get("backend") != backend:
            continue
        version = str(deployment.get("version", ""))
        if version:
            reason = str(metadata.get("operator_reason", "")).strip() or None
            values[version] = {
                "at": event.get("occurred_at"),
                "reason": reason,
            }
    return values


def _rollback_release(
    rows_by_version: dict[str, list[dict[str, Any]]],
    *,
    active_version: str | None,
    rolled_back: set[str],
) -> str | None:
    if active_version is None:
        return None
    active_semver = SemVer.parse(active_version)
    candidates = []
    for version, rows in rows_by_version.items():
        if (
            version == active_version
            or version in rolled_back
            or not _pair_complete(rows)
            or not all(row.get("accepted_at") for row in rows)
            or any(row.get("retired_at") for row in rows)
            or SemVer.parse(version).compare(active_semver) >= 0
        ):
            continue
        candidates.append((SemVer.parse(version), version))
    for _parsed, version in sorted(
        candidates,
        key=lambda item: (
            item[0].major,
            item[0].minor,
            item[0].patch,
            item[0].prerelease,
        ),
        reverse=True,
    ):
        return version
    return None


def _index_retained_release_records(
    intents: tuple[BackendIntent, ...],
    records: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Validate and index private accepted release records without ambiguity."""
    intents_by_slug = {intent.slug: intent for intent in intents}
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        backend = str(record.get("backend", ""))
        version = str(record.get("version", ""))
        base_intent = intents_by_slug.get(backend)
        if base_intent is None:
            raise ReleaseControlError(
                f"Retained release record names unknown backend {backend!r}"
            )
        SemVer.parse(version)
        historical_intent = BackendIntent(
            slug=base_intent.slug,
            provider_resource_slug=base_intent.provider_resource_slug,
            release_version=version,
            image_name=base_intent.image_name,
        )
        public_record = {
            field: value
            for field, value in record.items()
            if field != "_retained_sha256"
        }
        validate_release_record(public_record, intent=historical_intent)
        retained_sha256 = str(record.get("_retained_sha256", ""))
        if SHA256_PATTERN.fullmatch(retained_sha256) is None:
            raise ReleaseControlError(
                f"Retained {backend} {version} record lacks its exact SHA-256"
            )
        key = (backend, version)
        if key in indexed:
            raise ReleaseControlError(
                f"Duplicate retained release record for {backend} {version}"
            )
        indexed[key] = record
    return indexed


def _artifact_registry_digest(item: dict[str, Any]) -> str:
    """Extract the digest from gcloud's short or full version JSON shapes."""
    raw = str(item.get("digest") or item.get("version") or "")
    if "/versions/" in raw:
        raw = raw.rsplit("/versions/", 1)[1]
    elif "@" in raw:
        raw = raw.rsplit("@", 1)[1]
    return raw if IMAGE_DIGEST_PATTERN.fullmatch(raw) else ""


def calculate_status(
    intents: tuple[BackendIntent, ...],
    database: dict[str, Any],
    *,
    cloud_run: list[dict[str, Any]] | None = None,
    gar_images: list[dict[str, Any]] | None = None,
    release_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Calculate one read-only operator view per inventory backend."""
    if database.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ReleaseControlError(
            f"Database JSON must use schema_version {STATE_SCHEMA_VERSION}"
        )
    cloud_inventory_provided = cloud_run is not None
    image_inventory_provided = gar_images is not None
    release_inventory_provided = release_records is not None
    cloud_names = {
        str(item.get("name") or item.get("metadata", {}).get("name", ""))
        for item in (cloud_run or [])
    }
    gar_digests = {
        _artifact_registry_digest(item)
        for item in (gar_images or [])
        if _artifact_registry_digest(item)
    }
    retained = _index_retained_release_records(intents, release_records or [])
    rows = []
    for intent in intents:
        releases = _database_releases(database, backend=intent.slug)
        active_version = _active_release(releases)
        active_rows = releases.get(active_version or "", [])
        rolled_back = _rolled_back_versions(database, backend=intent.slug)
        rollback_version = _rollback_release(
            releases,
            active_version=active_version,
            rolled_back=set(rolled_back),
        )
        validators = [
            row
            for row in database.get("validators", [])
            if row.get("backend") == intent.slug
        ]
        active_validator_ids = {
            str(row.get("validator_id"))
            for row in active_rows
            if row.get("validator_id")
        }
        missing_validators = [
            {
                "validator_id": row.get("validator_id"),
                "slug": row.get("slug"),
                "version": row.get("version"),
            }
            for row in validators
            if str(row.get("validator_id")) not in active_validator_ids
        ]
        offered = SemVer.parse(intent.release_version)
        if active_version is None:
            action = "install offered release"
        else:
            comparison = offered.compare(SemVer.parse(active_version))
            if comparison < 0:
                action = "blocked: backends.toml version is older than active"
            elif intent.release_version in rolled_back:
                rollback_fact = rolled_back[intent.release_version]
                rollback_context = rollback_fact.get("at") or "unknown time"
                if rollback_fact.get("reason"):
                    rollback_context += f"; reason: {rollback_fact['reason']}"
                action = (
                    "blocked: offered release was previously rolled back from "
                    f"at {rollback_context}"
                )
            elif comparison > 0:
                action = "update to offered release"
            elif missing_validators:
                action = "reconcile missing semantic Validator deployment rows"
            else:
                action = "none"
        provider_missing = sorted(
            {
                str(row.get("provider_resource_name"))
                for row in active_rows
                if cloud_inventory_provided
                and row.get("provider_deleted_at") is None
                and str(row.get("provider_resource_name")).rsplit("/", 1)[-1]
                not in cloud_names
                and str(row.get("provider_resource_name")) not in cloud_names
            }
        )
        image_missing = sorted(
            {
                str(row.get("image_digest"))
                for row in active_rows
                if image_inventory_provided
                and row.get("image_digest") not in gar_digests
            }
        )
        retained_record = retained.get((intent.slug, active_version or ""))
        release_health = {}
        for version, release_rows in releases.items():
            activation_rows = [
                row for row in release_rows if row.get("retired_at") is None
            ]
            expected_validator_ids = {
                str(validator.get("validator_id"))
                for validator in validators
                if validator.get("validator_id")
            }
            missing_pair_validator_ids = []
            for validator_id in sorted(expected_validator_ids):
                validator_rows = [
                    row
                    for row in activation_rows
                    if str(row.get("validator_id")) == validator_id
                ]
                kinds = [row.get("kind") for row in validator_rows]
                if (
                    kinds.count("CLOUD_RUN_SERVICE") != 1
                    or kinds.count("CLOUD_RUN_JOB") != 1
                ):
                    missing_pair_validator_ids.append(validator_id)
            record = retained.get((intent.slug, version))
            row_record_digests = {
                str(row.get("release_record_sha256"))
                for row in release_rows
                if row.get("release_record_sha256")
            }
            row_image_digests = {
                str(row.get("image_digest"))
                for row in release_rows
                if row.get("image_digest")
            }
            record_digest = str(record.get("_retained_sha256", "")) if record else ""
            record_image = str(record.get("image_digest", "")) if record else ""
            release_health[version] = {
                "release_record_retained": record is not None,
                "release_record_matches_database": bool(record_digest)
                and row_record_digests == {record_digest},
                "release_record_image_matches_database": bool(record_image)
                and row_image_digests == {record_image},
                "image_present": (
                    bool(row_image_digests & gar_digests)
                    if image_inventory_provided
                    else None
                ),
                "provider_resources_present": all(
                    (
                        row.get("provider_deleted_at") is not None
                        or str(row.get("provider_resource_name")).rsplit("/", 1)[-1]
                        in cloud_names
                        or str(row.get("provider_resource_name")) in cloud_names
                    )
                    for row in release_rows
                )
                if cloud_inventory_provided
                else None,
                "missing_pair_validator_ids": missing_pair_validator_ids,
                "all_accepted": bool(activation_rows)
                and all(row.get("accepted_at") for row in activation_rows),
                "all_ready": bool(activation_rows)
                and all(row.get("readiness") == "READY" for row in activation_rows),
                "all_unblocked": bool(activation_rows)
                and all(row.get("blocked") is not True for row in activation_rows),
                "all_verified": bool(activation_rows)
                and all(
                    row.get("last_verification_succeeded") is True
                    for row in activation_rows
                ),
            }
        routing_mode = _routing_mode(active_rows)
        blockers = []
        if active_version:
            if routing_mode in {"inactive", "inconsistent"}:
                blockers.append(f"active release routing is {routing_mode}")
            if provider_missing:
                blockers.append("active provider resources are missing")
            if image_missing:
                blockers.append("active image digest is missing from GAR")
            if any(row.get("blocked") is True for row in active_rows):
                blockers.append("an active deployment is emergency blocked")
            if any(row.get("readiness") not in {None, "READY"} for row in active_rows):
                blockers.append("an active deployment is not READY")
            if any(
                row.get("last_verification_succeeded") is False for row in active_rows
            ):
                blockers.append("an active deployment failed its last verification")
            active_health = release_health.get(active_version, {})
            if release_inventory_provided and not active_health.get(
                "release_record_retained"
            ):
                blockers.append("active accepted release record is not retained")
            elif release_inventory_provided and (
                not active_health.get("release_record_matches_database")
                or not active_health.get("release_record_image_matches_database")
            ):
                blockers.append(
                    "active retained release record disagrees with database rows"
                )
        if blockers:
            action = f"blocked: {blockers[0]}"
        rows.append(
            {
                "backend": intent.slug,
                "file_version": intent.release_version,
                "active_version": active_version,
                "routing_mode": routing_mode,
                "rollback_version": rollback_version,
                "rolled_back_from": [
                    {
                        "version": version,
                        "at": fact.get("at"),
                        "reason": fact.get("reason"),
                    }
                    for version, fact in sorted(rolled_back.items())
                ],
                "candidate_versions": sorted(
                    version for version in releases if version != active_version
                ),
                "missing_validators": missing_validators,
                "unfinished_attempts": sum(
                    int(row.get("unfinished_attempts", 0))
                    for release_rows in releases.values()
                    for row in release_rows
                ),
                "provider_missing": provider_missing,
                "image_missing": image_missing,
                "blockers": blockers,
                "active_release_record_retained": (
                    retained_record is not None if active_version else None
                ),
                "release_health": release_health,
                "recommended_action": action,
            }
        )
    return {"schema_version": STATUS_SCHEMA_VERSION, "backends": rows}


def validate_activation_status(
    status: dict[str, Any],
    releases: dict[str, str],
) -> dict[str, Any]:
    """Fail unless a freshly calculated view supports every requested route."""
    if status.get("schema_version") != STATUS_SCHEMA_VERSION:
        raise ReleaseControlError(
            f"Status JSON must use schema_version {STATUS_SCHEMA_VERSION}"
        )
    if not releases:
        raise ReleaseControlError("At least one backend=version release is required")
    rows_by_backend = {
        str(row.get("backend")): row for row in status.get("backends", [])
    }
    verified = []
    for backend, version in sorted(releases.items()):
        row = rows_by_backend.get(backend)
        if row is None:
            raise ReleaseControlError(
                f"Activation preflight has no status row for {backend}"
            )
        failures = []
        if row.get("file_version") != version:
            failures.append(
                f"backends.toml now offers {row.get('file_version')!r}, not {version!r}"
            )
        health = (row.get("release_health") or {}).get(version)
        if health is None:
            failures.append("candidate release has no database deployment rows")
        else:
            required_true = {
                "provider_resources_present": "provider pair is absent from Cloud Run",
                "image_present": "image digest is absent from Artifact Registry",
                "release_record_retained": "accepted release record is not retained",
                "release_record_matches_database": (
                    "release-record digest disagrees with database rows"
                ),
                "release_record_image_matches_database": (
                    "release-record image disagrees with database rows"
                ),
                "all_accepted": "one or more deployment rows are not accepted",
                "all_ready": "one or more deployment rows are not READY",
                "all_unblocked": "one or more deployment rows are emergency blocked",
                "all_verified": (
                    "one or more deployment rows lack successful verification"
                ),
            }
            failures.extend(
                message
                for field, message in required_true.items()
                if health.get(field) is not True
            )
            missing = health.get("missing_pair_validator_ids") or []
            if missing:
                failures.append(
                    "semantic Validators lack an exact Service/Job pair: "
                    + ", ".join(str(value) for value in missing)
                )
        if failures:
            raise ReleaseControlError(
                f"Activation preflight failed for {backend} {version}: "
                + "; ".join(failures)
            )
        verified.append({"backend": backend, "version": version})
    return {
        "schema_version": ACTIVATION_CHECK_SCHEMA_VERSION,
        "releases": verified,
    }


def calculate_cleanup_plan(
    status: dict[str, Any],
    database: dict[str, Any],
    *,
    now: datetime | None = None,
    drain_days: int = DEFAULT_DRAIN_DAYS,
) -> dict[str, Any]:
    """Return exact deletions and safety reasons without deleting anything."""
    if drain_days < DEFAULT_DRAIN_DAYS:
        raise ReleaseControlError(
            f"Routine drain period cannot be below {DEFAULT_DRAIN_DAYS} days"
        )
    moment = now or datetime.now(tz=UTC)
    status_by_backend = {row["backend"]: row for row in status.get("backends", [])}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in database.get("deployments", []):
        key = (str(row.get("backend", "")), str(row.get("version", "")))
        if all(key):
            grouped.setdefault(key, []).append(row)
    delete = []
    retain = []
    for (backend, version), rows in sorted(grouped.items()):
        backend_status = status_by_backend.get(backend, {})
        reasons = []
        if version == backend_status.get("active_version"):
            reasons.append("active")
        if version == backend_status.get("rollback_version"):
            reasons.append("rollback release")
        if any(int(row.get("unfinished_attempts", 0)) for row in rows):
            reasons.append("unfinished attempt")
        if not _pair_complete(rows):
            reasons.append("incomplete deployment pair")
        if not all(row.get("accepted_at") for row in rows):
            reasons.append("release was not accepted")
        if any(row.get("retired_at") for row in rows):
            reasons.append("already retired")
        if any(
            row.get("kind") == "CLOUD_RUN_SERVICE"
            and int(row.get("minimum_instances", 0)) != 0
            for row in rows
        ):
            reasons.append("Service minimum instances is not zero")
        health = backend_status.get("release_health", {}).get(version, {})
        if not health.get("release_record_retained"):
            reasons.append("retained release record is missing")
        elif not health.get("release_record_matches_database"):
            reasons.append("retained release record digest does not match database")
        elif not health.get("release_record_image_matches_database"):
            reasons.append("retained release image does not match database")
        if health.get("image_present") is not True:
            reasons.append("protected image availability is unverified")
        if health.get("provider_resources_present") is not True:
            reasons.append("provider drift requires investigation")
        deactivated = [row.get("deactivated_at") for row in rows]
        if not all(deactivated):
            reasons.append("no complete inactivity period")
        else:
            earliest_eligible = max(
                datetime.fromisoformat(value).astimezone(UTC)
                + timedelta(days=drain_days)
                for value in deactivated
            )
            if moment < earliest_eligible:
                reasons.append(f"draining until {earliest_eligible.isoformat()}")
        item = {
            "backend": backend,
            "version": version,
            "resources": sorted(
                {
                    str(row.get("provider_resource_name"))
                    for row in rows
                    if row.get("provider_resource_name")
                }
            ),
        }
        if reasons:
            retain.append({**item, "reasons": reasons})
        else:
            delete.append(item)
    plan_body = {"delete": delete, "retain": retain}
    plan_id = (
        "cleanup-"
        + hashlib.sha256(
            json.dumps(plan_body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:8]
    )
    return {
        "schema_version": CLEANUP_SCHEMA_VERSION,
        "plan_id": plan_id,
        "drain_days": drain_days,
        **plan_body,
    }


def _intent_by_slug(
    intents: tuple[BackendIntent, ...],
    slug: str,
) -> BackendIntent:
    for intent in intents:
        if intent.slug == slug:
            return intent
    raise ReleaseControlError(f"Unknown release-enabled backend: {slug!r}")


def _selected_field(rows: list[dict[str, str]], field: str) -> str:
    """Return one known field from one selected inventory row."""
    if len(rows) != 1 or field not in rows[0]:
        raise ReleaseControlError("--field requires one backend and a known field")
    return rows[0][field]


def _release_arguments(values: list[str]) -> dict[str, str]:
    """Parse unique backend=version arguments for a local safety check."""
    releases = {}
    for value in values:
        backend, separator, version = value.partition("=")
        if not separator or not backend or not version:
            raise ReleaseControlError("--release must use backend=version")
        if backend in releases:
            raise ReleaseControlError(f"Duplicate backend release: {backend}")
        SemVer.parse(version)
        releases[backend] = version
    return releases


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=default_inventory_path(),
        help="Path to validibot-validator-backends/backends.toml",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    inventory = subcommands.add_parser("inventory")
    inventory.add_argument("--backend")
    inventory.add_argument("--field")
    name = subcommands.add_parser("name")
    name.add_argument("--backend", required=True)
    name.add_argument(
        "--version",
        help="Exact version override for rollback or historical resource lookup.",
    )
    name.add_argument("--kind", choices=["service", "job"], required=True)
    name.add_argument("--stage", choices=["prod", "staging", "dev"], required=True)
    preflight = subcommands.add_parser("release-record")
    preflight.add_argument("--backend", required=True)
    preflight.add_argument(
        "--version",
        help="Exact historical version expected in the release record.",
    )
    preflight.add_argument("--path", required=True, type=Path)
    status = subcommands.add_parser("status")
    status.add_argument("--database-json", required=True, type=Path)
    status.add_argument("--cloud-run-json", type=Path)
    status.add_argument("--gar-json", type=Path)
    status.add_argument("--release-records-json", type=Path)
    activation = subcommands.add_parser("activation-check")
    activation.add_argument("--status-json", required=True, type=Path)
    activation.add_argument("--release", action="append", required=True)
    cleanup = subcommands.add_parser("cleanup-plan")
    cleanup.add_argument("--database-json", required=True, type=Path)
    cleanup.add_argument("--status-json", required=True, type=Path)
    cleanup.add_argument("--drain-days", type=int, default=DEFAULT_DRAIN_DAYS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one read-only calculation and print stable JSON or one field."""
    try:
        options = _parser().parse_args(argv)
        intents = load_inventory(options.inventory)
        if options.command == "inventory":
            selected = (
                (_intent_by_slug(intents, options.backend),)
                if options.backend
                else intents
            )
            rows = [
                {
                    "backend": item.slug,
                    "provider_resource_slug": item.provider_resource_slug,
                    "release_version": item.release_version,
                    "source_tag": item.source_tag,
                    "image_name": item.image_name,
                    "service_name_prod": provider_resource_name(
                        item, kind="service", stage="prod"
                    ),
                    "job_name_prod": provider_resource_name(
                        item, kind="job", stage="prod"
                    ),
                }
                for item in selected
            ]
            if options.field:
                sys.stdout.write(_selected_field(rows, options.field) + "\n")
            else:
                sys.stdout.write(json.dumps(rows, indent=2, sort_keys=True) + "\n")
        elif options.command == "name":
            intent = _intent_by_slug(intents, options.backend)
            if options.version:
                intent = BackendIntent(
                    intent.slug,
                    intent.provider_resource_slug,
                    options.version,
                    intent.image_name,
                )
            sys.stdout.write(
                provider_resource_name(
                    intent,
                    kind=options.kind,
                    stage=options.stage,
                )
                + "\n"
            )
        elif options.command == "release-record":
            intent = _intent_by_slug(intents, options.backend)
            if options.version:
                intent = BackendIntent(
                    intent.slug,
                    intent.provider_resource_slug,
                    options.version,
                    intent.image_name,
                )
            sys.stdout.write(release_record_sha256(options.path, intent=intent) + "\n")
        elif options.command == "status":
            database = _load_json(options.database_json, expected_type=dict)
            result = calculate_status(
                intents,
                database,
                cloud_run=(
                    _load_json(options.cloud_run_json, expected_type=list)
                    if options.cloud_run_json
                    else None
                ),
                gar_images=(
                    _load_json(options.gar_json, expected_type=list)
                    if options.gar_json
                    else None
                ),
                release_records=(
                    _load_json(options.release_records_json, expected_type=list)
                    if options.release_records_json
                    else None
                ),
            )
            sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        elif options.command == "activation-check":
            status = _load_json(options.status_json, expected_type=dict)
            sys.stdout.write(
                json.dumps(
                    validate_activation_status(
                        status,
                        _release_arguments(options.release),
                    ),
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
        elif options.command == "cleanup-plan":
            database = _load_json(options.database_json, expected_type=dict)
            status = _load_json(options.status_json, expected_type=dict)
            sys.stdout.write(
                json.dumps(
                    calculate_cleanup_plan(
                        status,
                        database,
                        drain_days=options.drain_days,
                    ),
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
    except (OSError, ReleaseControlError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
