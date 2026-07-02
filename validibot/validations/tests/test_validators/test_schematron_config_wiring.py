"""Tests for Schematron pack wiring: the two-sided Ruleset guard + step flow.

Covers the ADR-2026-07-01 D5 data model at the DB/authoring layer. A pack is
**library content**: each vendored pack is a library ``Validator`` row whose
``default_ruleset`` is the global (`org=None`) pack ``Ruleset`` row. The
step's own ruleset is the author's assertion surface. That split gives
``Ruleset.clean()`` a two-sided contract:

- **Global SCHEMATRON rows** (pack rows) must reference a pack registered in
  ``packs.py`` with matching checksums — matching pins are denormalized into
  metadata as the provenance snapshot. A hand-crafted row cannot smuggle in
  an un-vetted artefact.
- **Org-owned SCHEMATRON rows** (per-step assertion surfaces created by
  ``ensure_advanced_ruleset``) need no pack pointer, and — like the global
  rows — may never carry inline rule content (arbitrary Schematron is
  arbitrary XSLT, i.e. code execution).

Also proves the step-authoring flow needs no Schematron-specific wiring:
``get_config_form_class`` falls through to ``BaseStepConfigForm`` and
``save_workflow_step`` creates the per-step assertion ruleset via its
existing ``ensure_advanced_ruleset`` fallback (ADR D2: pack selection is
validator selection).
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.validators.schematron.packs import SchematronPack
from validibot.validations.validators.schematron.packs import register_pack
from validibot.validations.validators.schematron.packs import unregister_pack
from validibot.workflows.forms import BaseStepConfigForm
from validibot.workflows.forms import get_config_form_class

PACK_ID = "vb-peppol-subset"
PACK_VERSION = "0.1.0"
SOURCE_SHA = "a" * 64
ARTIFACT_SHA = "b" * 64


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


def _global_pack_ruleset(**metadata) -> Ruleset:
    """Unsaved GLOBAL (org=None) SCHEMATRON ruleset — a pack row."""
    return Ruleset(
        org=None,
        name=PACK_ID,
        ruleset_type=RulesetType.SCHEMATRON,
        version=PACK_VERSION,
        metadata=metadata,
    )


# ── Global pack rows: the DB-layer allowlist enforcement ────────────────────


@pytest.mark.django_db
class TestGlobalPackRulesetClean:
    def test_bare_global_row_without_pointer_is_allowed(self):
        """A global SCHEMATRON ruleset with no pointer at all passes clean.

        ``Validator.ensure_default_ruleset()`` auto-creates exactly this
        shape for every validator row (including the Schematron engine row),
        so clean() must tolerate it. Safety is preserved elsewhere: such a
        row carries no content and launch-time resolution refuses no-pin
        rows.
        """
        _global_pack_ruleset().full_clean()  # must not raise

    def test_partial_pack_pointer_is_rejected(self, vb_pack):
        """A pointer with pack_id but no pack_version is rejected.

        A half-written pointer is either tampering or a vendoring bug; it
        must never save as if it were an intentional (absent or complete)
        state.
        """
        with pytest.raises(ValidationError, match="both"):
            _global_pack_ruleset(pack_id=PACK_ID).full_clean()

    def test_unregistered_pack_is_rejected(self):
        """A pointer to a pack absent from packs.py is rejected.

        This is half of "no arbitrary uploads": a hand-crafted global row
        cannot reference an artefact we never vetted.
        """
        with pytest.raises(ValidationError, match="not in the vetted"):
            _global_pack_ruleset(
                pack_id="rogue-pack",
                pack_version="9.9.9",
            ).full_clean()

    def test_checksum_drift_is_rejected(self, vb_pack):
        """A checksum snapshot disagreeing with the registry pin is rejected.

        If the metadata claims a different artefact hash than the vetted pin,
        either the row was tampered with or the registry moved under it —
        both must fail loudly, never execute.
        """
        with pytest.raises(ValidationError, match="does not match"):
            _global_pack_ruleset(
                pack_id=PACK_ID,
                pack_version=PACK_VERSION,
                pack_artifact_sha256="f" * 64,
            ).full_clean()

    def test_inline_rules_are_rejected(self, vb_pack):
        """Inline rules_text on any SCHEMATRON ruleset is rejected outright.

        Arbitrary Schematron is arbitrary XSLT (code execution). Pack
        content is delivered by artefact staging from the vendored file —
        rule content on the row is always smuggling.
        """
        ruleset = _global_pack_ruleset(
            pack_id=PACK_ID,
            pack_version=PACK_VERSION,
        )
        ruleset.rules_text = "<schema>rogue rules</schema>"
        with pytest.raises(ValidationError, match="never carry rule content"):
            ruleset.full_clean()

    def test_valid_pointer_denormalizes_pinned_checksums(self, vb_pack):
        """A valid pack row passes clean and snapshots the pinned checksums.

        The denormalized hashes are the row's provenance snapshot — what
        launch-time re-verification compares against — so they must be
        stamped from the registry pin, not left to the caller.
        """
        ruleset = _global_pack_ruleset(pack_id=PACK_ID, pack_version=PACK_VERSION)
        ruleset.full_clean()
        assert ruleset.metadata["pack_source_sha256"] == SOURCE_SHA
        assert ruleset.metadata["pack_artifact_sha256"] == ARTIFACT_SHA


# ── Org-owned rows: the per-step assertion surface ──────────────────────────


@pytest.mark.django_db
class TestStepAssertionRulesetClean:
    def test_org_owned_row_needs_no_pack_pointer(self):
        """An org-owned SCHEMATRON ruleset passes clean with no pointer.

        These rows are per-step assertion surfaces (ensure_advanced_ruleset
        creates them); pack identity lives on the step's validator FK, so
        demanding a pointer here would break ordinary step authoring.
        """
        from validibot.users.tests.factories import OrganizationFactory

        ruleset = Ruleset(
            org=OrganizationFactory(),
            name="step-assertions",
            ruleset_type=RulesetType.SCHEMATRON,
            version="1",
        )
        ruleset.full_clean()  # must not raise

    def test_org_owned_row_may_not_carry_rule_content(self):
        """Rule content on a step assertion ruleset is rejected.

        The no-smuggling posture applies on both sides: an org row with
        inline Schematron would be an un-vetted artefact one FK-swap away
        from execution.
        """
        from validibot.users.tests.factories import OrganizationFactory

        ruleset = Ruleset(
            org=OrganizationFactory(),
            name="step-assertions",
            ruleset_type=RulesetType.SCHEMATRON,
            version="1",
            rules_text="<schema>rogue rules</schema>",
        )
        with pytest.raises(ValidationError, match="never carry rule content"):
            ruleset.full_clean()


# ── Step authoring flow: no Schematron-specific wiring (ADR D2) ─────────────


def test_schematron_uses_the_base_step_config_form():
    """get_config_form_class falls through to BaseStepConfigForm.

    Pack selection is validator selection — there is deliberately no
    Schematron form in the mapping, so the wizard shows only the generic
    name/description/notes fields.
    """
    assert get_config_form_class(ValidationType.SCHEMATRON) is BaseStepConfigForm


@pytest.mark.django_db
def test_save_workflow_step_creates_a_per_step_assertion_ruleset():
    """Saving a Schematron step yields an org-owned assertion ruleset.

    This is the ensure_advanced_ruleset fallback doing its job with NO
    Schematron branch in save_workflow_step: the step gets its own
    org-owned SCHEMATRON ruleset (the author's assertion surface, deep-
    copied on workflow versioning), and nothing pack-related lands in the
    semantic config bucket — pack identity is the validator FK.
    """
    from validibot.validations.tests.factories import ValidatorFactory
    from validibot.workflows.tests.factories import WorkflowFactory
    from validibot.workflows.views_helpers import save_workflow_step

    workflow = WorkflowFactory()
    validator = ValidatorFactory(
        validation_type=ValidationType.SCHEMATRON,
        supports_assertions=True,
    )
    form = BaseStepConfigForm(data={"name": "EN 16931 rules"})
    assert form.is_valid(), form.errors

    step = save_workflow_step(workflow, validator, form)

    assert step.ruleset is not None
    assert step.ruleset.org == workflow.org
    assert step.ruleset.ruleset_type == RulesetType.SCHEMATRON
    assert step.ruleset.rules == ""
    assert step.config == {}
