"""
Tests for system validator configs and sync functionality.

The basic-sync tests below pin the historical happy-path. The
``SemanticDigest...`` and ``SlugVersionKeying...`` test groups cover
ADR-2026-04-27 Phase 3 Session B (tasks 7–9):

- ``sync_validators`` keys validator rows by ``(slug, version)`` so
  a config version bump creates a NEW row instead of mutating the
  old one.
- Each row stores a ``semantic_digest`` of the behavior-defining
  fields, computed on first sync.
- A subsequent sync with the same ``(slug, version)`` but a changed
  semantic config raises ``CommandError`` unless ``--allow-drift``
  is passed.
"""

import pytest
from django.core.management import CommandError
from django.core.management import call_command

from validibot.validations.constants import ValidationType
from validibot.validations.models import SignalDefinition
from validibot.validations.models import Validator
from validibot.validations.services.validator_digest import SHA256_HEX_LENGTH
from validibot.validations.services.validator_digest import compute_semantic_digest


@pytest.mark.django_db
def test_sync_validators_creates_energyplus():
    """The sync command should create the EnergyPlus validator
    and signal definitions.
    """
    # Ensure we start clean
    Validator.objects.filter(slug="energyplus-idf-validator").delete()

    call_command("sync_validators")

    validator = Validator.objects.get(slug="energyplus-idf-validator")
    assert validator.validation_type == ValidationType.ENERGYPLUS
    assert validator.processor_name == "EnergyPlus™ Simulation"
    assert validator.has_processor is True
    assert validator.is_system is True

    # Check signal definitions were created
    keys = set(
        validator.signal_definitions.values_list("contract_key", flat=True),
    )
    assert "site_electricity_kwh" in keys
    assert "heating_energy_kwh" in keys


@pytest.mark.django_db
def test_sync_validators_is_idempotent():
    """Running sync twice should not duplicate entries."""
    Validator.objects.filter(slug="energyplus-idf-validator").delete()

    call_command("sync_validators")
    initial_count = SignalDefinition.objects.filter(
        validator__slug="energyplus-idf-validator",
    ).count()

    call_command("sync_validators")
    final_count = SignalDefinition.objects.filter(
        validator__slug="energyplus-idf-validator",
    ).count()

    assert initial_count == final_count
    assert initial_count > 0


# ──────────────────────────────────────────────────────────────────────
# Phase 3 Session B: semantic digest population + drift detection
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_sync_populates_semantic_digest():
    """First sync stamps every validator row with a non-empty digest.

    Why: the digest is the foundation for drift detection. A row
    with an empty digest signals "uninitialised" — sync allows the
    empty → populated transition without raising. After that
    transition, ANY subsequent semantic change triggers drift.
    """
    Validator.objects.filter(slug="energyplus-idf-validator").delete()

    call_command("sync_validators")

    validator = Validator.objects.get(slug="energyplus-idf-validator")
    # 64-char hex SHA-256 means the digest was computed.
    assert validator.semantic_digest, "digest was not populated"
    assert len(validator.semantic_digest) == SHA256_HEX_LENGTH

    # Calling sync again must produce the same digest (determinism).
    call_command("sync_validators")
    validator.refresh_from_db()
    digest_after_2nd_sync = validator.semantic_digest

    call_command("sync_validators")
    validator.refresh_from_db()
    assert validator.semantic_digest == digest_after_2nd_sync


@pytest.mark.django_db
def test_sync_detects_drift_under_same_slug_version(monkeypatch):
    """Mutating semantic config under same (slug, version) -> CommandError.

    Why this matters: in production, a deploy that changes a
    validator's processor or class without a version bump would
    silently re-write the rules of every workflow that locked onto
    the old version. Sync's job is to make that loud — operators
    bump ``version`` to declare a new validator row instead.
    """
    Validator.objects.filter(slug="energyplus-idf-validator").delete()
    call_command("sync_validators")

    # Tamper with the stored digest to simulate the sync-time state
    # AFTER an in-place semantic change. (Mutating the actual config
    # at runtime is fragile because configs are frozen Pydantic
    # models; the digest is what sync's drift check compares.)
    validator = Validator.objects.get(slug="energyplus-idf-validator")
    validator.semantic_digest = "0" * 64  # valid-looking but wrong
    validator.save(update_fields=["semantic_digest"])

    with pytest.raises(CommandError, match="drift detected"):
        call_command("sync_validators")


@pytest.mark.django_db
def test_sync_allows_drift_with_flag():
    """``--allow-drift`` overrides the gate (development override).

    The ADR explicitly accepts the override path because the local
    dev loop ("tweak processor name, re-run sync") would otherwise
    require a version bump for every typo. Production deploys
    should never carry the flag.
    """
    Validator.objects.filter(slug="energyplus-idf-validator").delete()
    call_command("sync_validators")

    validator = Validator.objects.get(slug="energyplus-idf-validator")
    original_digest = validator.semantic_digest

    # Simulate drift by tampering with stored digest.
    validator.semantic_digest = "0" * 64
    validator.save(update_fields=["semantic_digest"])

    # With the flag, sync recovers and overwrites with the real digest.
    call_command("sync_validators", "--allow-drift")
    validator.refresh_from_db()
    assert validator.semantic_digest == original_digest


# ──────────────────────────────────────────────────────────────────────
# Phase 3 Session B task 7: (slug, version) keying
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_validator_uniqueness_is_slug_plus_version():
    """The DB enforces ``(slug, version)`` uniqueness, not slug alone.

    Belt-and-suspenders for the ADR's contract: even if a future
    code change accidentally re-introduces slug-only get_or_create,
    the unique constraint catches the regression.
    """
    # Existing constraint check via model meta — pre-existed,
    # but Phase 3 task 7 makes sync_validators ACTUALLY use it.
    constraint_names = {c.name for c in Validator._meta.constraints}
    assert "uq_validator_slug_version" in constraint_names


@pytest.mark.django_db
def test_sync_does_not_create_duplicate_rows_for_unchanged_config():
    """A no-op sync produces zero new rows.

    Idempotency check: the (slug, version) keying must mean a
    repeat sync hits the existing row rather than creating
    additional ones. Without (slug, version) keying, a sync after
    schema changes that produced different ``defaults`` could leak
    rows.
    """
    Validator.objects.filter(slug="energyplus-idf-validator").delete()
    call_command("sync_validators")

    count_before = Validator.objects.filter(
        slug="energyplus-idf-validator",
    ).count()

    call_command("sync_validators")
    call_command("sync_validators")

    count_after = Validator.objects.filter(
        slug="energyplus-idf-validator",
    ).count()
    assert count_before == count_after == 1


@pytest.mark.django_db
def test_digest_helper_matches_sync_population():
    """A digest computed by hand equals the one sync stored.

    Cross-check: this is the audit story (Session D) in miniature —
    re-compute the digest from the config, compare to what's in the
    DB, and they MUST match. If they ever diverge the audit
    command's output would be unreliable.
    """
    from validibot.validations.validators.base.config import get_all_configs

    Validator.objects.filter(slug="energyplus-idf-validator").delete()
    call_command("sync_validators")

    cfg = next(c for c in get_all_configs() if c.slug == "energyplus-idf-validator")
    expected_digest = compute_semantic_digest(cfg.model_dump())

    validator = Validator.objects.get(slug="energyplus-idf-validator")
    assert validator.semantic_digest == expected_digest
