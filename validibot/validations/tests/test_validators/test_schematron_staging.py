"""Tests for Schematron pack-artefact staging verification (ADR-2026-07-01 D4b).

``staging.verified_pack_artifact_path`` is the single verification gate both
dispatch paths (Cloud Run upload, local Docker workspace materialisation)
pass through before delivering a rule pack to the container. The invariant:
**no artefact leaves Django unless its bytes match the registry pin.** A
checkout drifted from ``packs.py`` (wrong file, edited file, missing file)
must fail fast with an infrastructure error — never stage silently, because
the container would then reject it (or worse, an unverified container build
would run it).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from validibot.validations.validators.schematron.packs import SchematronPack
from validibot.validations.validators.schematron.packs import (
    SchematronPackResolutionError,
)
from validibot.validations.validators.schematron.staging import (
    verified_pack_artifact_path,
)

# The subset fixture doubles as a stand-in vendored artefact: it is a real
# repo file whose true sha256 we can compute for the happy path.
FIXTURE_ARTIFACT = "tests/assets/schematron/peppol_billing_subset.sch"


def _pack(*, artifact: str, artifact_sha256: str) -> SchematronPack:
    """Build a pack descriptor without touching the global registry.

    Staging verifies against the descriptor it is handed — registration is
    irrelevant here, so these tests stay registry-clean.
    """
    return SchematronPack(
        id="vb-peppol-subset",
        title="VB Peppol subset",
        version="0.1.0",
        syntax="ubl",
        source_url="https://example.test/packs/vb-peppol-subset",
        license="MIT",
        query_binding="xslt1",
        artifact=artifact,
        source_sha256="a" * 64,
        artifact_sha256=artifact_sha256,
        engine="lxml-xslt1",
    )


def test_matching_checksum_returns_the_repo_path():
    """A vendored artefact whose bytes match the pin resolves to its path.

    The returned path is what the dispatchers stage (upload to GCS /
    materialise into the workspace), so it must be the repo-relative file
    the registry pinned — nothing copied, nothing rewritten.
    """
    true_sha = hashlib.sha256(Path(FIXTURE_ARTIFACT).read_bytes()).hexdigest()
    pack = _pack(artifact=FIXTURE_ARTIFACT, artifact_sha256=true_sha)

    path = verified_pack_artifact_path(pack)

    assert path.is_file()
    assert path.read_bytes() == Path(FIXTURE_ARTIFACT).read_bytes()


def test_missing_artifact_fails_fast():
    """A pack whose artefact file is absent from the checkout is refused.

    This is the "operator forgot to vendor the file" failure: the registry
    entry exists but the artefact doesn't. Staging must name the expected
    path so the operator can fix the checkout.
    """
    pack = _pack(
        artifact="tests/assets/schematron/does_not_exist.xslt",
        artifact_sha256="b" * 64,
    )
    with pytest.raises(SchematronPackResolutionError, match=r"not\s+found"):
        verified_pack_artifact_path(pack)


def test_checksum_drift_is_refused():
    """An artefact whose bytes don't match the registry pin is refused.

    The scenario D4b exists for: the file on disk was edited (or the wrong
    release was vendored) without updating the code-reviewed pin. Executing
    it would void the whole provenance story, so staging refuses.
    """
    pack = _pack(artifact=FIXTURE_ARTIFACT, artifact_sha256="c" * 64)
    with pytest.raises(SchematronPackResolutionError, match="drifted"):
        verified_pack_artifact_path(pack)
