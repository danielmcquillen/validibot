"""Tests for Schematron rules resolution at launch time (ADR-2026-07-01 D4b).

``resolve_schematron_inputs`` builds the typed container inputs by resolving
the author's rules text — from the step's ruleset (where the step-config
upload stored it), falling back to the validator's ``default_ruleset`` for
library validators that bundle rules. The rules ship INLINE (the SHACL
shapes_text pattern) with a sha256 provenance stamp; a step that resolves to
no rules refuses to dispatch.

Skips as a module when validibot-shared < 0.12.0 (the inline-rules
contract); activates automatically once the released package is synced.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from validibot_shared.schematron.envelopes import SchematronInputs

from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.models import Ruleset
from validibot.validations.models import Validator

if "schematron_text" not in SchematronInputs.model_fields:
    pytest.skip(
        "requires validibot-shared >= 0.12.0 (inline Schematron rules contract)",
        allow_module_level=True,
    )

from validibot.validations.validators.schematron import launch

ASSETS = Path("tests/assets/schematron")
SCH_TEXT = (ASSETS / "peppol_billing_subset.sch").read_text()


def _step_ruleset(rules_text: str = SCH_TEXT) -> Ruleset:
    """An org-owned step ruleset carrying the author's rules (unsaved OK)."""
    return Ruleset(
        name="step-rules",
        ruleset_type=RulesetType.SCHEMATRON,
        version="1",
        rules_text=rules_text,
    )


@pytest.mark.django_db
class TestResolveSchematronInputs:
    def test_step_rules_ship_inline_with_provenance_and_limits(self):
        """The typed inputs carry the rules text, its sha256, and D8 limits.

        The sha256 computed at dispatch is the run's provenance identity —
        the container echoes it back so a result can always be tied to the
        exact rules that produced it.
        """
        from validibot.validations.tests.factories import ValidatorFactory

        validator = ValidatorFactory(validation_type=ValidationType.SCHEMATRON)
        inputs = launch.resolve_schematron_inputs(
            validator=validator,
            ruleset=_step_ruleset(),
        )

        assert inputs.schematron_text == SCH_TEXT.strip()
        assert (
            inputs.schematron_sha256
            == hashlib.sha256(
                SCH_TEXT.strip().encode("utf-8"),
            ).hexdigest()
        )
        assert inputs.max_findings > 0
        assert inputs.xslt_timeout_seconds > 0

    def test_library_default_rules_are_the_fallback(self):
        """A library validator's bundled rules apply when the step has none.

        This is the SHACL library-validator pattern: an org can publish a
        reusable validator whose default_ruleset carries the rules, and
        steps using it need not upload anything.
        """
        from validibot.validations.tests.factories import ValidatorFactory

        default_ruleset = Ruleset.objects.create(
            name="library-rules",
            ruleset_type=RulesetType.SCHEMATRON,
            version="1",
            rules_text=SCH_TEXT,
        )
        validator = ValidatorFactory(
            validation_type=ValidationType.SCHEMATRON,
            default_ruleset=default_ruleset,
        )

        inputs = launch.resolve_schematron_inputs(validator=validator, ruleset=None)

        assert "VB-CO-15" in inputs.schematron_text

    def test_no_rules_anywhere_refuses_to_dispatch(self):
        """A step resolving to no rules raises instead of dispatching.

        The form requires rules at save time, so this guards fixtures,
        imports, and emptied rulesets: a run with nothing to check must
        error loudly, never launch a container to validate against nothing.
        (Built without the factory: ``Validator.save()`` auto-creates an
        empty default ruleset, which is exactly the no-rules fallback case
        this exercises.)
        """
        validator = Validator.objects.create(
            slug="schematron-empty",
            name="Empty schematron validator",
            validation_type=ValidationType.SCHEMATRON,
            version=1,
        )

        with pytest.raises(
            launch.SchematronRulesResolutionError,
            match="No Schematron rules",
        ):
            launch.resolve_schematron_inputs(validator=validator, ruleset=None)
