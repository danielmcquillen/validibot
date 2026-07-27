"""Tests for the local validator backend release controller.

The controller is the only public application-repository program that reads
the sibling backend inventory. These tests prove provider naming, SemVer
direction, rollback-aware recommendations, and drain-plan calculations remain
deterministic without contacting Django or GCP.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "validator_release_control.py"
SPEC = importlib.util.spec_from_file_location("validator_release_control", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = control
SPEC.loader.exec_module(control)

WORKSPACE = Path(__file__).parents[4]
INVENTORY = WORKSPACE / "validibot-validator-backends" / "backends.toml"
DIGEST = "sha256:" + "a" * 64
EXPECTED_BACKEND_COUNT = 5
EXPECTED_PROVIDER_RESOURCE_COUNT = 10
EXPECTED_PAIR_MEMBER_COUNT = 2
SHA256_HEX_LENGTH = 64


def _deployment(
    *,
    backend: str,
    version: str,
    kind: str,
    role: str,
    deactivated_at: str | None = None,
    cause: str = "",
    attempts: int = 0,
) -> dict[str, object]:
    """Return one safe database projection used by status and cleanup."""
    return {
        "deployment_id": f"{backend}-{version}-{kind}",
        "validator_id": f"{backend}-validator",
        "backend": backend,
        "version": version,
        "kind": kind,
        "routing_role": role,
        "accepted_at": "2026-01-01T00:00:00+00:00",
        "deactivated_at": deactivated_at,
        "deactivation_cause": cause,
        "readiness": "READY",
        "blocked": False,
        "last_verification_succeeded": True,
        "retired_at": None,
        "provider_deleted_at": None,
        "unfinished_attempts": attempts,
        "provider_resource_name": f"projects/p/locations/r/{kind.lower()}/{backend}",
        "image_digest": DIGEST,
        "release_record_sha256": "b" * SHA256_HEX_LENGTH,
    }


def _database(deployments: list[dict[str, object]]) -> dict[str, object]:
    """Build one valid remote database export around selected deployments."""
    backends = sorted({str(row["backend"]) for row in deployments})
    return {
        "schema_version": control.STATE_SCHEMA_VERSION,
        "validators": [
            {
                "validator_id": f"{backend}-validator",
                "slug": f"{backend}-validator",
                "version": 1,
                "backend": backend,
            }
            for backend in backends
        ],
        "deployments": deployments,
        "routing_events": [],
    }


def test_inventory_builds_all_five_bounded_release_specific_pairs():
    """Every release-enabled inventory row must produce unique production names."""
    intents = control.load_inventory(INVENTORY)

    names = {
        control.provider_resource_name(intent, kind=kind, stage="prod")
        for intent in intents
        for kind in ("service", "job")
    }

    assert len(intents) == EXPECTED_BACKEND_COUNT
    assert len(names) == EXPECTED_PROVIDER_RESOURCE_COUNT
    assert "vb-vs-energyplus-v0-15-1" in names
    assert "vb-vj-portfolio-manager-v0-16-1" in names
    assert all(len(name) <= control.MAX_PROVIDER_NAME_LENGTH for name in names)


def test_development_names_are_mutable_but_staging_keeps_release_identity():
    """Only development omits the version; staging remains release-specific."""
    intent = control._intent_by_slug(control.load_inventory(INVENTORY), "shacl")

    assert (
        control.provider_resource_name(intent, kind="service", stage="dev")
        == "vb-vs-shacl-dev"
    )
    assert (
        control.provider_resource_name(intent, kind="job", stage="staging")
        == "vb-vj-shacl-v0-15-1-stg"
    )


def test_provider_names_encode_prerelease_and_build_boundaries_without_collision():
    """Distinct valid SemVer identities must never select the same resource."""
    prerelease = control.BackendIntent(
        "shacl",
        "shacl",
        "1.2.3-alpha.1",
        "validibot-validator-backend-shacl",
    )
    build = control.BackendIntent(
        "shacl",
        "shacl",
        "1.2.3+alpha.1",
        "validibot-validator-backend-shacl",
    )

    prerelease_name = control.provider_resource_name(
        prerelease,
        kind="job",
        stage="prod",
    )
    build_name = control.provider_resource_name(build, kind="job", stage="prod")

    assert prerelease_name == "vb-vj-shacl-v1-2-3-pre-alpha-1"
    assert build_name == "vb-vj-shacl-v1-2-3-build-alpha-1"
    assert prerelease_name != build_name


def test_name_builder_refuses_truncation_for_an_oversized_provider_slug():
    """An invalid Cloud Run name must fail before any provider command runs."""
    intent = control.BackendIntent(
        "backend",
        "x" * 60,
        "1.2.3",
        "validibot-validator-backend-backend",
    )

    with pytest.raises(control.ReleaseControlError, match="exceeds"):
        control.provider_resource_name(intent, kind="service", stage="prod")


def test_historical_release_record_uses_an_explicit_version_override(tmp_path):
    """Deep recovery must verify the older tag instead of the offered version."""
    intent = control.BackendIntent(
        "shacl",
        "shacl",
        "0.14.0",
        "validibot-validator-backend-shacl",
    )
    record = {
        "schema_version": "validibot.backend-release.v1",
        "backend": "shacl",
        "version": "0.14.0",
        "source_tag": "shacl-v0.14.0",
        "source_commit": "1" * 40,
        "image": "ghcr.io/example/validibot-validator-backend-shacl",
        "image_digest": DIGEST,
        "shared_contract": "0.20.0",
        "sbom": "validibot-validator-backend-shacl.spdx.json",
        "build_verification": "github-actions:run-1",
    }
    path = tmp_path / "release.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    digest = control.release_record_sha256(path, intent=intent)

    assert len(digest) == SHA256_HEX_LENGTH


def test_empty_provider_and_image_inventories_fail_closed():
    """A successful empty GCP read means active resources are missing, not unknown."""
    intent = control.BackendIntent(
        "energyplus",
        "energyplus",
        "0.18.0",
        "validibot-validator-backend-energyplus",
    )
    deployments = [
        _deployment(
            backend="energyplus",
            version="0.18.0",
            kind="CLOUD_RUN_SERVICE",
            role="PRIMARY",
        ),
        _deployment(
            backend="energyplus",
            version="0.18.0",
            kind="CLOUD_RUN_JOB",
            role="LONG_RUNNING",
        ),
    ]

    status = control.calculate_status(
        (intent,),
        _database(deployments),
        cloud_run=[],
        gar_images=[],
    )
    row = status["backends"][0]

    assert len(row["provider_missing"]) == EXPECTED_PAIR_MEMBER_COUNT
    assert row["image_missing"] == [DIGEST]
    assert row["release_health"]["0.18.0"]["provider_resources_present"] is False
    assert row["release_health"]["0.18.0"]["image_present"] is False
    assert row["release_health"]["0.18.0"]["missing_pair_validator_ids"] == []
    assert row["release_health"]["0.18.0"]["all_accepted"] is True
    assert row["release_health"]["0.18.0"]["all_ready"] is True
    assert row["release_health"]["0.18.0"]["all_unblocked"] is True
    assert row["release_health"]["0.18.0"]["all_verified"] is True
    assert row["recommended_action"].startswith("blocked:")
    assert "active provider resources are missing" in row["blockers"]


def test_status_rejects_a_retained_record_outside_the_strict_public_schema():
    """Private retention metadata must not conceal a drifted release document."""
    intent = control.BackendIntent(
        "shacl",
        "shacl",
        "0.15.1",
        "validibot-validator-backend-shacl",
    )
    record = {
        "schema_version": "validibot.backend-release.v1",
        "backend": "shacl",
        "version": "0.15.1",
        "source_tag": "shacl-v0.15.1",
        "source_commit": "1" * 40,
        "image": "ghcr.io/example/validibot-validator-backend-shacl",
        "image_digest": DIGEST,
        "shared_contract": "0.20.0",
        "sbom": "validibot-validator-backend-shacl.spdx.json",
        "build_verification": "github-actions:run-1",
        "application_sbom": "unexpected-extra-field.json",
        "_retained_sha256": "b" * SHA256_HEX_LENGTH,
    }

    with pytest.raises(control.ReleaseControlError, match="fields differ"):
        control.calculate_status(
            (intent,),
            _database([]),
            release_records=[record],
        )


def test_status_extracts_digest_from_gcloud_artifact_version_resource_name():
    """GAR's full version path must corroborate the database image digest."""
    intent = control.BackendIntent(
        "energyplus",
        "energyplus",
        "0.18.0",
        "validibot-validator-backend-energyplus",
    )
    deployments = [
        _deployment(
            backend="energyplus",
            version="0.18.0",
            kind="CLOUD_RUN_SERVICE",
            role="PRIMARY",
        ),
        _deployment(
            backend="energyplus",
            version="0.18.0",
            kind="CLOUD_RUN_JOB",
            role="LONG_RUNNING",
        ),
    ]

    status = control.calculate_status(
        (intent,),
        _database(deployments),
        gar_images=[
            {
                "version": (
                    "projects/p/locations/r/repositories/validators/packages/"
                    f"energyplus/versions/{DIGEST}"
                )
            }
        ],
    )

    assert status["backends"][0]["image_missing"] == []
    assert status["backends"][0]["release_health"]["0.18.0"]["image_present"] is True


def test_status_uses_semver_and_suppresses_a_rolled_back_offered_release():
    """A file version escaped by rollback must never become routine advice."""
    intent = control.BackendIntent(
        "energyplus",
        "energyplus",
        "0.18.0",
        "validibot-validator-backend-energyplus",
    )
    deployments = [
        _deployment(
            backend="energyplus",
            version="0.17.0",
            kind="CLOUD_RUN_SERVICE",
            role="PRIMARY",
        ),
        _deployment(
            backend="energyplus",
            version="0.17.0",
            kind="CLOUD_RUN_JOB",
            role="LONG_RUNNING",
        ),
        _deployment(
            backend="energyplus",
            version="0.18.0",
            kind="CLOUD_RUN_SERVICE",
            role="INACTIVE",
            deactivated_at="2026-07-20T00:00:00+00:00",
            cause="RELEASE_ROLLBACK_FROM",
        ),
        _deployment(
            backend="energyplus",
            version="0.18.0",
            kind="CLOUD_RUN_JOB",
            role="INACTIVE",
            deactivated_at="2026-07-20T00:00:00+00:00",
            cause="RELEASE_ROLLBACK_FROM",
        ),
    ]

    database = _database(deployments)
    database["routing_events"] = [
        {
            "occurred_at": "2026-07-20T00:00:00+00:00",
            "deployment_id": "energyplus-0.18.0-CLOUD_RUN_SERVICE",
            "changes": {
                "deactivation_cause": ["", "RELEASE_ROLLBACK_FROM"],
            },
            "metadata": {
                "backend_slug": "energyplus",
                "backend_release": "0.17.0",
                "operator_reason": "Service callbacks were returning 502.",
            },
        }
    ]

    status = control.calculate_status((intent,), database)
    row = status["backends"][0]

    assert row["active_version"] == "0.17.0"
    assert row["routing_mode"] == "normal"
    assert row["recommended_action"].startswith("blocked:")
    assert [fact["version"] for fact in row["rolled_back_from"]] == ["0.18.0"]
    assert row["rolled_back_from"][0]["version"] == "0.18.0"
    assert row["rolled_back_from"][0]["reason"] == (
        "Service callbacks were returning 502."
    )
    assert "Service callbacks were returning 502." in row["recommended_action"]


def test_rollback_history_checks_only_the_new_deactivation_cause():
    """A superseded rollback cause must not permanently suppress a release."""
    deployment = _deployment(
        backend="energyplus",
        version="0.18.0",
        kind="CLOUD_RUN_SERVICE",
        role="INACTIVE",
        deactivated_at="2026-07-21T00:00:00+00:00",
        cause="OPERATOR_DEACTIVATION",
    )
    database = _database([deployment])
    database["routing_events"] = [
        {
            "occurred_at": "2026-07-21T00:00:00+00:00",
            "deployment_id": deployment["deployment_id"],
            "changes": {
                "deactivation_cause": [
                    "RELEASE_ROLLBACK_FROM",
                    "OPERATOR_DEACTIVATION",
                ],
            },
            "metadata": {
                "backend_slug": "energyplus",
                "backend_release": "0.18.0",
            },
        }
    ]

    assert control._rolled_back_versions(database, backend="energyplus") == {}


def test_activation_check_rejects_changed_provider_observations():
    """Route activation must use a post-acceptance view that still passes."""
    healthy = {
        "schema_version": control.STATUS_SCHEMA_VERSION,
        "backends": [
            {
                "backend": "energyplus",
                "file_version": "0.18.0",
                "release_health": {
                    "0.18.0": {
                        "provider_resources_present": True,
                        "image_present": True,
                        "release_record_retained": True,
                        "release_record_matches_database": True,
                        "release_record_image_matches_database": True,
                        "missing_pair_validator_ids": [],
                        "all_accepted": True,
                        "all_ready": True,
                        "all_unblocked": True,
                        "all_verified": True,
                    }
                },
            }
        ],
    }

    result = control.validate_activation_status(
        healthy,
        {"energyplus": "0.18.0"},
    )

    assert result["releases"] == [{"backend": "energyplus", "version": "0.18.0"}]

    drifted = json.loads(json.dumps(healthy))
    drifted["backends"][0]["release_health"]["0.18.0"]["provider_resources_present"] = (
        False
    )
    with pytest.raises(control.ReleaseControlError, match="absent from Cloud Run"):
        control.validate_activation_status(
            drifted,
            {"energyplus": "0.18.0"},
        )


def test_cleanup_plan_protects_active_rollback_and_unfinished_releases():
    """Only complete accepted pairs past seven days may enter the deletion plan."""
    inactive_at = "2026-07-01T00:00:00+00:00"
    deployments = []
    for version, role_service, role_job, attempts in (
        ("0.18.0", "PRIMARY", "LONG_RUNNING", 0),
        ("0.17.0", "INACTIVE", "INACTIVE", 0),
        ("0.16.0", "INACTIVE", "INACTIVE", 1),
        ("0.15.0", "INACTIVE", "INACTIVE", 0),
    ):
        deployments.extend(
            [
                _deployment(
                    backend="energyplus",
                    version=version,
                    kind="CLOUD_RUN_SERVICE",
                    role=role_service,
                    deactivated_at=(
                        None if role_service != "INACTIVE" else inactive_at
                    ),
                    attempts=attempts,
                ),
                _deployment(
                    backend="energyplus",
                    version=version,
                    kind="CLOUD_RUN_JOB",
                    role=role_job,
                    deactivated_at=None if role_job != "INACTIVE" else inactive_at,
                    attempts=attempts,
                ),
            ]
        )
    database = _database(deployments)
    status = {
        "schema_version": control.STATUS_SCHEMA_VERSION,
        "backends": [
            {
                "backend": "energyplus",
                "active_version": "0.18.0",
                "rollback_version": "0.17.0",
                "release_health": {
                    version: {
                        "release_record_retained": True,
                        "release_record_matches_database": True,
                        "release_record_image_matches_database": True,
                        "image_present": True,
                        "provider_resources_present": True,
                    }
                    for version in ("0.18.0", "0.17.0", "0.16.0", "0.15.0")
                },
            }
        ],
    }

    plan = control.calculate_cleanup_plan(
        status,
        database,
        now=datetime(2026, 7, 24, tzinfo=UTC),
    )

    assert [row["version"] for row in plan["delete"]] == ["0.15.0"]
    retained = {row["version"]: row["reasons"] for row in plan["retain"]}
    assert "active" in retained["0.18.0"]
    assert "rollback release" in retained["0.17.0"]
    assert "unfinished attempt" in retained["0.16.0"]
    assert plan["plan_id"].startswith("cleanup-")
