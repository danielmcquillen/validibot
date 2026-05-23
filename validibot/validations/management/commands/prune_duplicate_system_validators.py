"""
One-shot cleanup for duplicate system Validator rows.

Why this exists
---------------
The Validator table is keyed by ``UniqueConstraint(slug, version)``, not by
``slug`` alone. When a validator config's ``version`` field is bumped (e.g.
the SHACL validator went from an early version to ``0.2``), ``sync_validators``
creates a new row at the new ``(slug, version)`` and leaves the old row in
place. The leftover row keeps appearing in the "Add workflow step" picker as
a duplicate card.

Self-hosted operators upgrading across one of those bumps end up with two
"SHACL Validator" cards (one from the old version, one from the new) and no
clean Django-migration way to merge them without risking PROTECT'd FKs from
``WorkflowStep``.

What it does
------------
For each ``slug`` that has more than one system Validator row, this command:

  1. Picks a "canonical" row — the one whose ``version`` matches the
     currently-declared config version. If no config is found, falls back to
     the row with the highest ``version`` string (lexicographic).
  2. Reassigns every FK from the stale rows to the canonical row:
       - ``WorkflowStep.validator`` (PROTECT'd; must be moved before delete)
       - ``ValidatorResourceFile.validator`` (CASCADE; moved to preserve data)
       - assertion / binding / trace references to stale ``StepIODefinition``
         rows, remapped to matching canonical signal definitions before the
         stale validator is deleted
  3. Drops the stale row. Remaining stale ``StepIODefinition`` and
     ``Derivation`` rows attached to the stale validator are CASCADE-deleted;
     ``sync_validators`` has already populated the canonical row with the
     up-to-date signal/derivation set.

Custom org-owned validators (``is_system=False``) are never touched. This
command only operates on system validators.

Usage
-----
    # Show what would change without writing anything (default).
    python manage.py prune_duplicate_system_validators

    # Actually perform the deletion.
    python manage.py prune_duplicate_system_validators --commit
"""

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from validibot.validations.models import Validator
from validibot.validations.validators.base.config import get_all_configs


class Command(BaseCommand):
    help = (
        "Find and merge duplicate system Validator rows that share a slug "
        "but differ in version (one-shot data fix; safe to re-run)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            default=False,
            help=(
                "Apply the changes. Without this flag the command runs in "
                "dry-run mode and reports what it would do."
            ),
        )

    def handle(self, *args, **options):
        commit = options["commit"]
        mode_label = "COMMIT" if commit else "DRY-RUN"
        self.stdout.write(self.style.WARNING(f"Mode: {mode_label}"))

        # Build a lookup of declared canonical versions from configs so we
        # can prefer the row whose version matches the current source of
        # truth, rather than blindly keeping the highest version string.
        declared_versions = {cfg.slug: cfg.version for cfg in get_all_configs()}

        groups: dict[str, list[Validator]] = defaultdict(list)
        for v in Validator.objects.filter(is_system=True).order_by("slug", "version"):
            groups[v.slug].append(v)

        had_duplicates = False
        for slug, rows in groups.items():
            if len(rows) <= 1:
                continue
            had_duplicates = True

            canonical = self._pick_canonical(slug, rows, declared_versions)
            stale = [r for r in rows if r.pk != canonical.pk]

            self.stdout.write("")
            self.stdout.write(
                self.style.NOTICE(
                    f"slug={slug!r}: {len(rows)} rows — "
                    f"keep id={canonical.pk} version={canonical.version!r}, "
                    f"drop {[(r.pk, r.version) for r in stale]}",
                ),
            )

            if commit:
                with transaction.atomic():
                    self._merge_rows(canonical, stale)
                self.stdout.write(self.style.SUCCESS("  merged."))
            else:
                self._preview_merge(canonical, stale)

        if not had_duplicates:
            self.stdout.write(
                self.style.SUCCESS("No duplicate system validators found."),
            )
        elif not commit:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "Dry-run complete. Re-run with --commit to apply the merge.",
                ),
            )

    # ────────────────────────────────────────────────────────────────────
    # Internals
    # ────────────────────────────────────────────────────────────────────

    def _pick_canonical(
        self,
        slug: str,
        rows: list[Validator],
        declared_versions: dict[str, str],
    ) -> Validator:
        """Prefer the row whose version matches the config; else highest."""
        declared = declared_versions.get(slug)
        if declared:
            for r in rows:
                if r.version == declared:
                    return r
        # Fallback: highest version string (lexicographic — fine for "1.0",
        # "0.2" style values; an unhelpful tiebreaker but a deterministic one).
        return max(rows, key=lambda r: (r.version or "", r.pk))

    def _preview_merge(self, canonical: Validator, stale: list[Validator]) -> None:
        """Report FK counts that would be reassigned, for dry-run output."""
        for r in stale:
            step_count = (
                r.workflowstep_set.count() if hasattr(r, "workflowstep_set") else 0
            )
            resource_count = (
                r.resource_files.count() if hasattr(r, "resource_files") else 0
            )
            signal_remaps, unmapped_signals = self._signal_remap_counts(
                canonical,
                r,
            )
            self.stdout.write(
                f"    would move {step_count} workflow step(s) and "
                f"{resource_count} resource file(s), and remap "
                f"{signal_remaps} signal reference(s) from id={r.pk} "
                f"→ id={canonical.pk}",
            )
            if unmapped_signals:
                self.stdout.write(
                    self.style.WARNING(
                        "    stale signal(s) without canonical match: "
                        f"{unmapped_signals}",
                    ),
                )

    def _merge_rows(self, canonical: Validator, stale: list[Validator]) -> None:
        """Reassign FKs from each stale row to canonical, then delete stale."""
        from validibot.validations.models import ValidatorResourceFile
        from validibot.workflows.models import WorkflowStep

        for r in stale:
            self._remap_signal_references(canonical, r)
            # WorkflowStep uses on_delete=PROTECT so we MUST move first.
            WorkflowStep.objects.filter(validator=r).update(validator=canonical)
            ValidatorResourceFile.objects.filter(validator=r).update(
                validator=canonical,
            )
            # StepIODefinition / Derivation CASCADE; the canonical already
            # has its own (refreshed) set via sync_validators, so dropping
            # the stale validator's set is the right behavior.
            r.delete()

    def _canonical_signal_map(self, canonical: Validator) -> dict[tuple[str, str], int]:
        """Map canonical validator signals by the identity authors reference."""
        from validibot.validations.models import StepIODefinition

        return {
            (sig.contract_key, sig.direction): sig.pk
            for sig in StepIODefinition.objects.filter(validator=canonical)
        }

    def _signal_replacements(
        self,
        canonical: Validator,
        stale: Validator,
    ) -> tuple[dict[int, int], list[tuple[str, str]]]:
        """Return ``{stale_signal_id: canonical_signal_id}`` plus misses."""
        from validibot.validations.models import StepIODefinition

        canonical_signals = self._canonical_signal_map(canonical)
        replacements: dict[int, int] = {}
        unmapped: list[tuple[str, str]] = []
        for sig in StepIODefinition.objects.filter(validator=stale):
            key = (sig.contract_key, sig.direction)
            canonical_id = canonical_signals.get(key)
            if canonical_id:
                replacements[sig.pk] = canonical_id
            else:
                unmapped.append(key)
        return replacements, unmapped

    def _signal_remap_counts(
        self,
        canonical: Validator,
        stale: Validator,
    ) -> tuple[int, list[tuple[str, str]]]:
        """Count FK rows that would be remapped for dry-run output."""
        from validibot.validations.models import ResolvedInputTrace
        from validibot.validations.models import RulesetAssertion
        from validibot.validations.models import StepInputBinding

        replacements, unmapped = self._signal_replacements(canonical, stale)
        stale_ids = list(replacements)
        if not stale_ids:
            return 0, unmapped
        count = (
            RulesetAssertion.objects.filter(
                target_signal_definition_id__in=stale_ids,
            ).count()
            + StepInputBinding.objects.filter(
                signal_definition_id__in=stale_ids,
            ).count()
            + ResolvedInputTrace.objects.filter(
                signal_definition_id__in=stale_ids,
            ).count()
        )
        return count, unmapped

    def _remap_signal_references(self, canonical: Validator, stale: Validator) -> None:
        """Move references off stale validator-owned signal definitions."""
        from validibot.validations.models import ResolvedInputTrace
        from validibot.validations.models import RulesetAssertion
        from validibot.validations.models import StepInputBinding

        replacements, _unmapped = self._signal_replacements(canonical, stale)
        for stale_signal_id, canonical_signal_id in replacements.items():
            RulesetAssertion.objects.filter(
                target_signal_definition_id=stale_signal_id,
            ).update(target_signal_definition_id=canonical_signal_id)
            ResolvedInputTrace.objects.filter(
                signal_definition_id=stale_signal_id,
            ).update(signal_definition_id=canonical_signal_id)

            # ``StepInputBinding`` has a uniqueness constraint on
            # (workflow_step, signal_definition). Resolve rare collisions
            # explicitly instead of letting the cleanup abort halfway through.
            for binding in list(
                StepInputBinding.objects.filter(signal_definition_id=stale_signal_id),
            ):
                duplicate_exists = (
                    StepInputBinding.objects.filter(
                        workflow_step_id=binding.workflow_step_id,
                        signal_definition_id=canonical_signal_id,
                    )
                    .exclude(pk=binding.pk)
                    .exists()
                )
                if duplicate_exists:
                    binding.delete()
                else:
                    binding.signal_definition_id = canonical_signal_id
                    binding.save(update_fields=["signal_definition", "modified"])
