"""Tests for the legacy ``prune_duplicate_system_validators`` command.

Validator rows are now integer-versioned, and multiple rows with the same slug
represent legitimate version history. The library hides older versions from
normal browsing, so the old "same slug means duplicate card" cleanup is no
longer allowed to merge or delete rows. These tests keep that operator command
non-destructive for anyone who still has it in a runbook.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from validibot.validations.constants import ValidationType
from validibot.validations.models import Validator
from validibot.validations.tests.factories import ValidatorFactory


class PruneDuplicateSystemValidatorsTests(TestCase):
    """The legacy prune command must preserve validator version history."""

    def _call(self, *args):
        """Run the command and capture stdout/stderr for assertions."""
        out, err = StringIO(), StringIO()
        call_command(
            "prune_duplicate_system_validators",
            *args,
            stdout=out,
            stderr=err,
        )
        return out.getvalue(), err.getvalue()

    def test_reports_multi_version_family_without_mutating_rows(self):
        """A dry run should describe version history but leave every row intact."""
        v1 = ValidatorFactory(
            slug="shacl-validator",
            version=1,
            validation_type=ValidationType.SHACL,
            is_system=True,
        )
        v2 = ValidatorFactory(
            slug="shacl-validator",
            version=2,
            validation_type=ValidationType.SHACL,
            is_system=True,
        )

        out, _ = self._call()

        self.assertIn("DRY-RUN", out)
        self.assertIn("versioned row", out)
        self.assertTrue(Validator.objects.filter(pk=v1.pk).exists())
        self.assertTrue(Validator.objects.filter(pk=v2.pk).exists())

    def test_commit_flag_is_backward_compatible_but_non_destructive(self):
        """Even with ``--commit``, older versions are preserved.

        The flag remains accepted so old deployment scripts do not crash, but
        it must not collapse valid version history into the latest row.
        """
        v1 = ValidatorFactory(
            slug="energyplus-idf-validator",
            version=1,
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )
        v3 = ValidatorFactory(
            slug="energyplus-idf-validator",
            version=3,
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )

        out, _ = self._call("--commit")

        self.assertIn("COMMIT", out)
        self.assertIn("No rows were changed", out)
        self.assertTrue(Validator.objects.filter(pk=v1.pk).exists())
        self.assertTrue(Validator.objects.filter(pk=v3.pk).exists())

    def test_single_version_families_report_clean(self):
        """A database with no version history should produce a clear clean result."""
        ValidatorFactory(slug="basic-validator", version=1, is_system=True)

        out, _ = self._call()

        self.assertIn("No multi-version system validator families found", out)
