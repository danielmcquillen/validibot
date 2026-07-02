"""Tests for Schematron pack resolution at launch time (ADR-2026-07-01 D4b/D5).

``resolve_schematron_inputs`` builds the typed container inputs from the
step's **library validator**: the pack pointer lives on
``validator.default_ruleset`` (the global pack row the vendoring command
materialised), mirroring how ``resolve_shacl_inputs`` reads a library
validator's default ruleset for shapes. These tests pin that resolution and
its refusal modes — launch must never proceed past a missing, unregistered,
or drifted pack, because executing an unpinned artefact would void the
entire provenance story.

Skips as a module when validibot-shared < 0.11.0 (no
``validibot_shared.schematron``); activates automatically once the released
package is synced into the venv.
"""

from __future__ import annotations

import pytest

from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.validators.schematron.packs import SchematronPack
from validibot.validations.validators.schematron.packs import register_pack
from validibot.validations.validators.schematron.packs import unregister_pack

launch = pytest.importorskip(
    "validibot.validations.validators.schematron.launch",
    reason="requires validibot-shared >= 0.11.0 (validibot_shared.schematron)",
)

PACK_ID = "vb-peppol-subset"
PACK_VERSION = "0.1.0"
SOURCE_SHA = "a" * 64
ARTIFACT_SHA = "b" * 64
STAGED_URI = "gs://bucket/runs/run-1/pack.xslt"


@pytest.fixture
def vb_pack():
    """Register a temporary vetted pack for the duration of a test."""
    pack = SchematronPack(
        id=PACK_ID,
        title="VB Peppol subset",
        version=PACK_VERSION,
        syntax="ubl",
        source_url="https://example.test/packs/vb-peppol-subset",
        license="MIT",
        query_binding="xslt1",
        artifact="tests/assets/schematron/peppol_billing_subset.sch",
        source_sha256=SOURCE_SHA,
        artifact_sha256=ARTIFACT_SHA,
        engine="lxml-xslt1",
    )
    register_pack(pack)
    yield pack
    unregister_pack(pack.id, pack.version)


def _pack_validator(vb_pack):
    """Build a library pack validator: global pack row as default_ruleset."""
    from validibot.validations.tests.factories import ValidatorFactory

    pack_ruleset = Ruleset(
        org=None,
        name=vb_pack.id,
        ruleset_type=RulesetType.SCHEMATRON,
        version=vb_pack.version,
        metadata={"pack_id": vb_pack.id, "pack_version": vb_pack.version},
    )
    pack_ruleset.full_clean()
    pack_ruleset.save()
    return ValidatorFactory(
        validation_type=ValidationType.SCHEMATRON,
        default_ruleset=pack_ruleset,
    )


@pytest.mark.django_db
class TestResolveSchematronInputs:
    def test_resolves_pin_checksums_and_limits_from_the_library_validator(
        self,
        vb_pack,
    ):
        """The typed inputs carry the pin, checksums, binding, and D8 limits.

        The container verifies the staged artefact against
        ``artifact_sha256`` before executing (D4b) — so the value MUST come
        from the registry pin via the validator's default_ruleset, never
        from anything the step or submission could influence.
        """
        validator = _pack_validator(vb_pack)

        inputs = launch.resolve_schematron_inputs(
            validator=validator,
            artifact_uri=STAGED_URI,
        )

        assert inputs.pack_id == PACK_ID
        assert inputs.pack_version == PACK_VERSION
        assert inputs.artifact_uri == STAGED_URI
        assert inputs.artifact_sha256 == ARTIFACT_SHA
        assert inputs.source_sha256 == SOURCE_SHA
        assert inputs.query_binding == "xslt1"
        assert inputs.engine == "lxml-xslt1"
        # D8 defaults ride along, already clamped Django-side.
        assert inputs.max_findings > 0
        assert inputs.xslt_timeout_seconds > 0

    def test_validator_without_default_ruleset_refuses_to_launch(self):
        """A pack validator with no default_ruleset fails resolution.

        This state is defensive (``Validator.save()`` auto-creates a
        default ruleset), but remains reachable — the FK can be nulled by a
        ruleset deletion — so resolution must still refuse loudly rather
        than crash or run unpinned. Uses an unsaved instance to model the
        null-FK state without fighting the save hook.
        """
        from validibot.validations.models import Validator

        validator = Validator(
            slug="schematron-orphan",
            name="Orphan pack validator",
            validation_type=ValidationType.SCHEMATRON,
            version=1,
        )

        with pytest.raises(
            launch.SchematronPackResolutionError,
            match="no default_ruleset",
        ):
            launch.resolve_schematron_inputs(
                validator=validator,
                artifact_uri=STAGED_URI,
            )

    def test_default_ruleset_without_pack_pointer_refuses_to_launch(self):
        """A default_ruleset lacking the pack pin fails resolution.

        Guards the factory/fixture path: a Schematron validator whose
        default ruleset exists but carries no pack pointer (e.g. created by
        generic tooling) must refuse at launch, not run an unpinned
        artefact. ValidatorFactory's auto-created default ruleset is exactly
        this shape.
        """
        from validibot.validations.tests.factories import ValidatorFactory

        validator = ValidatorFactory(validation_type=ValidationType.SCHEMATRON)

        with pytest.raises(
            launch.SchematronPackResolutionError,
            match="no pack_id/pack_version",
        ):
            launch.resolve_schematron_inputs(
                validator=validator,
                artifact_uri=STAGED_URI,
            )

    def test_unregistering_a_pack_after_vendoring_refuses_to_launch(
        self,
        vb_pack,
    ):
        """A pack row whose registry entry vanished fails resolution.

        ``Ruleset.clean()`` enforced the pin at save time; this guards the
        launch-time race (registry changed under a saved row). The step must
        error, not silently run whatever artefact is lying around.
        """
        validator = _pack_validator(vb_pack)
        unregister_pack(PACK_ID, PACK_VERSION)
        try:
            with pytest.raises(
                launch.SchematronPackResolutionError,
                match="not in the vetted",
            ):
                launch.resolve_schematron_inputs(
                    validator=validator,
                    artifact_uri=STAGED_URI,
                )
        finally:
            register_pack(vb_pack)  # restore for fixture teardown

    def test_checksum_drift_between_row_and_registry_refuses_to_launch(
        self,
        vb_pack,
    ):
        """A pack row snapshot disagreeing with the registry pin is refused.

        Defence in depth for the provenance story: if the row says one
        artefact hash and the registry says another, something moved —
        never execute a drifted artefact.
        """
        validator = _pack_validator(vb_pack)
        # Simulate drift by tampering the saved snapshot (bypassing clean).
        ruleset = validator.default_ruleset
        ruleset.metadata = {
            **ruleset.metadata,
            "pack_artifact_sha256": "f" * 64,
        }
        ruleset.save(update_fields=["metadata"])
        validator.refresh_from_db()

        with pytest.raises(
            launch.SchematronPackResolutionError,
            match="refusing to run a drifted artefact",
        ):
            launch.resolve_schematron_inputs(
                validator=validator,
                artifact_uri=STAGED_URI,
            )
