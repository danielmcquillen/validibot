"""Tests for the ``vendor_schematron_packs`` command (ADR-2026-07-01 D5).

The vendoring command is the bridge from the code-reviewed allowlist
(``packs.py``) to the library data model: one global pack ``Ruleset`` row +
one library ``Validator`` row (with the engine's ``o.*`` catalog) per pack
version. These tests pin the lifecycle guarantees the design depends on:

- **Idempotence** — re-running vendors nothing twice.
- **Row identity is the pin** — a new pack version creates NEW rows with a
  bumped integer revision; the old rows (which steps reference by FK) are
  untouched.
- **Immutability** — a registry pin that disagrees with an existing row, or
  artefact bytes that disagree with the pin, abort the command before any
  write.
- **Sync exemption** — pack validators carry ``config_provider=""`` so the
  ``sync_validators`` missing-config sweep can never mark them unavailable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import StepIODefinition
from validibot.validations.models import Validator
from validibot.validations.validators.base.config import get_config
from validibot.validations.validators.schematron.packs import SchematronPack
from validibot.validations.validators.schematron.packs import register_pack
from validibot.validations.validators.schematron.packs import unregister_pack

pytestmark = pytest.mark.django_db

FIXTURE_ARTIFACT = "tests/assets/schematron/peppol_billing_subset.sch"
FIXTURE_SHA = hashlib.sha256(Path(FIXTURE_ARTIFACT).read_bytes()).hexdigest()
PACK_ID = "vb-peppol-subset"


def _pack(version: str, *, artifact_sha256: str = FIXTURE_SHA) -> SchematronPack:
    return SchematronPack(
        id=PACK_ID,
        title="VB Peppol subset",
        version=version,
        syntax="ubl",
        source_url="https://example.test/packs/vb-peppol-subset",
        license="MIT",
        query_binding="xslt1",
        artifact=FIXTURE_ARTIFACT,
        source_sha256="a" * 64,
        artifact_sha256=artifact_sha256,
        engine="lxml-xslt1",
    )


@pytest.fixture
def vb_pack():
    pack = _pack("0.1.0")
    register_pack(pack)
    yield pack
    unregister_pack(pack.id, pack.version)


def test_vendoring_creates_pack_ruleset_validator_and_catalog(vb_pack):
    """One command run materialises the full D5 projection of a pack.

    Global pack ruleset (org=None, denormalized pins), library validator
    (org=None, is_system, engine fields from the SCHEMATRON config,
    default_ruleset = the pack row, sync-exempt), and one StepIODefinition
    per engine catalog entry — the signal machinery reads these per
    validator row, so every pack validator needs its own copy.
    """
    call_command("vendor_schematron_packs", pack=[f"{PACK_ID}@0.1.0"])

    ruleset = Ruleset.objects.get(
        org=None,
        ruleset_type=RulesetType.SCHEMATRON,
        name=PACK_ID,
        version="0.1.0",
    )
    assert ruleset.metadata["pack_artifact_sha256"] == FIXTURE_SHA
    assert ruleset.metadata["license"] == "MIT"

    validator = Validator.objects.get(slug=f"schematron-{PACK_ID}")
    assert validator.validation_type == ValidationType.SCHEMATRON
    assert validator.default_ruleset == ruleset
    assert validator.org is None
    assert validator.is_system is True
    assert validator.version == 1
    assert validator.config_provider == ""  # sync-sweep exemption
    assert "VB Peppol subset" in validator.name

    engine_config = get_config(ValidationType.SCHEMATRON)
    signal_count = StepIODefinition.objects.filter(validator=validator).count()
    assert signal_count == len(engine_config.catalog_entries)


def test_vendoring_is_idempotent(vb_pack):
    """Running the command twice creates no duplicate rows.

    Operators run this on every deploy (like sync_validators); a second run
    must recognise the existing pin and leave row identity untouched.
    """
    call_command("vendor_schematron_packs")
    call_command("vendor_schematron_packs")

    assert Validator.objects.filter(slug=f"schematron-{PACK_ID}").count() == 1
    assert (
        Ruleset.objects.filter(
            org=None,
            ruleset_type=RulesetType.SCHEMATRON,
            name=PACK_ID,
        ).count()
        == 1
    )


def test_new_pack_version_creates_new_rows_and_leaves_old_untouched(vb_pack):
    """Vendoring a newer pack version bumps the revision, never mutates.

    This IS the D5 lifecycle: steps pin concrete validator rows by FK, so
    the May-and-November upstream cadence must always append rows. The old
    row keeps its default_ruleset (and thus its exact artefact pin).
    """
    call_command("vendor_schematron_packs")

    newer = _pack("0.2.0")
    register_pack(newer)
    try:
        call_command("vendor_schematron_packs", pack=[f"{PACK_ID}@0.2.0"])
    finally:
        unregister_pack(newer.id, newer.version)

    rows = list(
        Validator.objects.filter(slug=f"schematron-{PACK_ID}").order_by("version"),
    )
    assert [row.version for row in rows] == [1, 2]
    assert rows[0].default_ruleset.version == "0.1.0"
    assert rows[1].default_ruleset.version == "0.2.0"


def test_drifted_artifact_aborts_before_any_write(vb_pack):
    """Artefact bytes that don't match the registry pin abort the command.

    The verification runs before any DB write, so a drifted checkout can
    never half-materialise a pack.
    """
    drifted = _pack("0.3.0", artifact_sha256="d" * 64)
    register_pack(drifted)
    try:
        with pytest.raises(CommandError, match="drifted"):
            call_command("vendor_schematron_packs", pack=[f"{PACK_ID}@0.3.0"])
    finally:
        unregister_pack(drifted.id, drifted.version)

    assert not Ruleset.objects.filter(
        org=None,
        ruleset_type=RulesetType.SCHEMATRON,
        name=PACK_ID,
        version="0.3.0",
    ).exists()


def test_unknown_pack_selection_is_a_clear_error(vb_pack):
    """--pack pointing at an unregistered id@version fails with guidance."""
    with pytest.raises(CommandError, match="not registered"):
        call_command("vendor_schematron_packs", pack=["nope@9.9.9"])
