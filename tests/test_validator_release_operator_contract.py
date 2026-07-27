"""Static tests for the independent validator release operator interface.

The recipes ultimately invoke cloud CLIs, which these tests must never run.
Instead, they pin the safety-critical command construction: the private hosted
wrappers provide the exact project and region, routine operations expose five
commands, and provider deployment creates release-specific resources from
digest-selected images without updating a stable validator Job in place.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings

PUBLIC_GCP_RECIPES = Path(settings.BASE_DIR) / "just" / "gcp" / "mod.just"
PRIVATE_JUSTFILE = Path(settings.BASE_DIR).parent / "validibot-project" / "Justfile"
HOSTED_PROJECT = "project-a509c806-3e21-4fbc-b19"
HOSTED_REGION = "australia-southeast1"
ROUTINE_COMMANDS = (
    "validator-setup",
    "validator-status",
    "validator-update",
    "validator-rollback",
    "validator-cleanup",
)
EXPECTED_OUTGOING_PROVIDER_REVERIFY_COUNT = 2


def _recipe(text: str, name: str, next_marker: str) -> str:
    """Return one recipe body without parsing or executing Just syntax."""

    start = text.index(name)
    end = text.index(next_marker, start)
    return text[start:end]


def test_private_hosted_wrappers_supply_exact_production_coordinates():
    """Routine commands must not ask operators to retype project or region."""

    text = PRIVATE_JUSTFILE.read_text(encoding="utf-8")

    assert f'gcp_project := "{HOSTED_PROJECT}"' in text
    assert f'gcp_region := "{HOSTED_REGION}"' in text
    for command in ROUTINE_COMMANDS:
        assert re.search(rf"(?m)^{re.escape(command)}(?:[ :]|$)", text)
        assert f"just gcp {command} prod" in text


def test_release_job_recipe_creates_one_digest_selected_named_resource():
    """A backend release must never rewrite a stable validator Job definition."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    recipe = _recipe(
        text,
        "validator-job-deploy name stage",
        "# Deploy all managed validator Jobs",
    )

    assert 'name --backend "{{name}}" --version "$BACKEND_RELEASE"' in recipe
    assert 'IMAGE_REF="${IMAGE_REPOSITORY}@${IMAGE_DIGEST}"' in recipe
    assert 'gcloud run jobs create "$JOB_NAME"' in recipe
    assert "gcloud run jobs update" not in recipe
    assert "gcloud run jobs delete" not in recipe
    assert "VALIDIBOT_BACKEND_SLUG={{name}}" in recipe
    assert 'VALIDIBOT_SOURCE_RELEASE_TAG="$SOURCE_TAG"' in recipe
    assert 'VALIDIBOT_RELEASE_RECORD_SHA256="$RELEASE_RECORD_SHA"' in recipe
    assert 'revision="$DEPLOYMENT_REVISION"' in recipe


def test_release_service_and_job_share_release_identity_environment():
    """Both pair members must expose values the read-only importers compare."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    job = _recipe(
        text,
        "validator-job-deploy name stage",
        "# Deploy all managed validator Jobs",
    )
    service = _recipe(
        text,
        "validator-service-deploy name stage",
        "# Provision all managed Services",
    )
    required = (
        "VALIDIBOT_BACKEND_SLUG={{name}}",
        'VALIDIBOT_BACKEND_IMAGE_DIGEST="$IMAGE_DIGEST"',
        'VALIDIBOT_BACKEND_RELEASE="$BACKEND_RELEASE"',
        'VALIDIBOT_SOURCE_RELEASE_TAG="$SOURCE_TAG"',
        'VALIDIBOT_RELEASE_RECORD_SHA256="$RELEASE_RECORD_SHA"',
    )

    for value in required:
        assert value in job
        assert value in service
    assert "latest" not in job
    assert "latest" not in service


def test_cleanup_reads_retained_accepted_release_records_from_every_stage():
    """Accepted image protection must outlive each stage's provider resources."""

    text = PRIVATE_JUSTFILE.read_text(encoding="utf-8")
    recipe = _recipe(
        text,
        "gcp-validator-images-cleanup",
        "\ndocs:",
    )

    assert "for STAGE in prod staging dev" in recipe
    assert 'STORAGE_BUCKET="validibot-storage"' in recipe
    assert 'STORAGE_BUCKET="validibot-storage-${STAGE}"' in recipe
    assert "operations/validator-backend-releases/*/*.json" in recipe
    assert '--release-records "$RELEASE_RECORDS"' in recipe


def test_setup_and_multi_update_activate_selected_backends_as_one_group():
    """A final route failure must not leave a partially active backend set."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    setup = _recipe(
        text,
        "validator-setup stage",
        "# Reconcile one backend",
    )
    update = _recipe(
        text,
        'validator-update stage backend=""',
        "# Roll back one backend",
    )

    for recipe in (setup, update):
        assert "activate_validator_backend_release_group" in recipe
        assert "--release=${" in recipe
        assert "_validator-status-json" in recipe
        assert "activation-check" in recipe
        assert "validator-deployments-sync" in recipe
        assert "validator-services-register" in recipe
        assert 'management-cmd {{stage}} "$GROUP_COMMAND"' in recipe


def test_update_reverifies_and_round_trips_the_outgoing_release():
    """Candidate acceptance must prove the advertised rollback pair still works."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    update = _recipe(
        text,
        'validator-update stage backend=""',
        "# Roll back one backend",
    )

    assert 'old_source_tag="${selected_backend}-v${old_version}"' in update
    assert 'validator-release-verify "$selected_backend"' in update
    assert (
        update.count(
            'validator-deployments-sync "$selected_backend" {{stage}} \\\n'
            '                "$old_version"'
        )
        == EXPECTED_OUTGOING_PROVIDER_REVERIFY_COUNT
    )
    rollback = (
        'validator-backend-route "$selected_backend" {{stage}} \\\n'
        '                "$old_version" normal OPERATOR_DEACTIVATION'
    )
    restore_candidate = (
        'validator-backend-route "$selected_backend" {{stage}} \\\n'
        '                "$version" normal OPERATOR_DEACTIVATION'
    )
    assert rollback in update
    assert restore_candidate in update
    assert update.index(rollback) < update.index(restore_candidate)


def test_exact_recovery_requires_and_transports_a_recorded_repair_reason():
    """Reusing a rolled-back version must create accountable audit metadata."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    rollback = _recipe(
        text,
        'validator-rollback stage backend operation="release"',
        "# Calculate an exact seven-day cleanup plan",
    )
    route = _recipe(
        text,
        "validator-backend-route name stage version",
        "# Change mutable service-level warming",
    )

    assert ".rolled_back_from[]?" in rollback
    assert "Explain what was repaired before reusing this release" in rollback
    assert "reusing a rolled-back release requires a reason" in rollback
    assert "base64 | tr -d" in rollback
    assert '"$CAUSE" "" "$RECOVERY_REASON_B64"' in rollback
    assert "--reason-b64={{reason_b64}}" in route


def test_release_preflight_names_each_missing_publication_artifact():
    """Operators must see the exact absent trust artifact before GCP mutation."""

    text = PUBLIC_GCP_RECIPES.read_text(encoding="utf-8")
    verifier = _recipe(
        text,
        "_validator-release-verify-image name release_tag",
        "# Verify the signed release and GAR mirror",
    )

    required_messages = (
        "Missing: signed Git tag $SOURCE_TAG",
        "Missing: GHCR image",
        "Missing or invalid: image build attestation",
        "Missing: GitHub Release $SOURCE_TAG",
        '"backend release JSON"',
        '"release JSON checksum"',
        '"SPDX SBOM"',
        "Missing or invalid: attestation for backend release JSON",
        "Missing: Artifact Registry mirror",
        "No GCP resources were changed.",
    )
    for message in required_messages:
        assert message in verifier
