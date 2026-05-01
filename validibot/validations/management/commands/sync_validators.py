"""
Management command to sync system validators and their signal definitions.

Usage:
    python manage.py sync_validators
    python manage.py sync_validators --allow-drift  # development override

All system validators — both built-in single-file validators (Basic, JSON
Schema, XML Schema, AI Assist) and package-based validators (EnergyPlus,
FMU, THERM) — declare their metadata via ``ValidatorConfig``. This command
discovers all configs and ensures the corresponding ``Validator``,
``SignalDefinition``, and ``Derivation`` rows exist in the database.

The signal definitions are required for the step editor UI to show separate
"Input Assertions" and "Output Assertions" sections.

ADR-2026-04-27 Phase 3, Session B (tasks 7–9): the command keys validator
rows by ``(slug, version)`` rather than ``slug`` alone, and computes a
``semantic_digest`` from the validator's behavior-defining fields. If a
config is changed in place under the same ``(slug, version)`` (e.g. a
processor name swapped without a version bump), sync raises a
``CommandError`` so the operator must either bump ``version`` to declare
a new validator row, or pass ``--allow-drift`` if they're in development
and intentionally re-syncing a mutated config.
"""

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction

from validibot.validations.constants import SignalOriginKind
from validibot.validations.models import Derivation
from validibot.validations.models import SignalDefinition
from validibot.validations.models import Validator
from validibot.validations.services.catalog_entry_normalization import (
    build_provider_binding_from_mapping,
)
from validibot.validations.services.validator_digest import compute_semantic_digest
from validibot.validations.validators.base.config import get_all_configs


class Command(BaseCommand):
    help = (
        "Sync system validators and their signal definitions from config declarations."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--allow-drift",
            action="store_true",
            default=False,
            help=(
                "Allow re-syncing a validator whose semantic config has "
                "changed under the same (slug, version). Use only in "
                "development. In production this signals an unbumped "
                "version — bump the config's ``version`` instead."
            ),
        )

    def handle(self, *args, **options):
        configs = get_all_configs()
        allow_drift = options["allow_drift"]

        total_validators_created = 0
        total_validators_updated = 0
        total_signals_synced = 0
        total_derivations_synced = 0

        for cfg in configs:
            self.stdout.write(f"Processing {cfg.slug}...")

            with transaction.atomic():
                # Build validator field dict from the Pydantic model,
                # excluding fields that aren't Validator model columns.
                # This must cover ALL ValidatorConfig fields that don't
                # map to a Validator DB column — if a new config field
                # is added, add it here too.
                validator_data = cfg.model_dump(
                    exclude={
                        "allowed_extensions",
                        "card_image",
                        "catalog_entries",
                        "icon",
                        "image_name",
                        "output_envelope_class",
                        "provider",
                        "resolved_class",
                        "resolved_envelope_class",
                        "resource_types",
                        "step_editor_cards",
                        "validator_class",
                    },
                )

                # Compute the semantic digest from the FULL config dump
                # (not the trimmed one above). The digest function picks
                # only SEMANTIC_FIELDS — passing the trimmed dump would
                # silently exclude fields like ``catalog_entries`` and
                # ``validator_class`` that are highly semantic.
                proposed_digest = compute_semantic_digest(cfg.model_dump())

                # ADR-2026-04-27 Phase 3 task 7: key by (slug, version),
                # not slug alone. A version bump in the config now
                # creates a new Validator row instead of mutating the
                # old one — preserving the launch contract that
                # workflows locked onto the old version were running
                # under.
                validator, created = Validator.objects.get_or_create(
                    slug=cfg.slug,
                    version=cfg.version,
                    defaults={**validator_data, "semantic_digest": proposed_digest},
                )

                if created:
                    total_validators_created += 1
                    self.stdout.write(
                        self.style.SUCCESS(f"  Created validator: {validator}"),
                    )
                else:
                    # ADR-2026-04-27 Phase 3 task 9: drift detection.
                    # If the existing row's stored digest disagrees
                    # with the proposed digest, the config changed
                    # behavior without bumping ``version``. That's
                    # a contract violation: workflows that locked
                    # onto this (slug, version) would silently start
                    # running under different rules.
                    #
                    # Empty stored digest is the migration-window
                    # case: a row created before semantic_digest
                    # existed. We allow the empty → populated
                    # transition without raising; that's the
                    # backfill the field was designed for.
                    existing_digest = validator.semantic_digest or ""
                    if (
                        existing_digest
                        and existing_digest != proposed_digest
                        and not allow_drift
                    ):
                        msg = (
                            f"Semantic drift detected on validator "
                            f"{cfg.slug} v{cfg.version}: stored digest "
                            f"{existing_digest[:12]}... differs from "
                            f"proposed {proposed_digest[:12]}.... "
                            f"Either bump the config's ``version`` to "
                            f"declare a new validator row, or re-run "
                            f"with ``--allow-drift`` to overwrite "
                            f"(development only)."
                        )
                        raise CommandError(msg)

                    # Update existing validator fields, including the
                    # newly-computed digest.
                    for key, value in validator_data.items():
                        if key not in {"slug", "version"}:
                            setattr(validator, key, value)
                    validator.semantic_digest = proposed_digest
                    validator.save()
                    total_validators_updated += 1
                    if existing_digest and existing_digest != proposed_digest:
                        # Allowed-drift path: log loudly so operators
                        # see what just got rewritten.
                        self.stdout.write(
                            self.style.WARNING(
                                f"  Updated validator (DRIFT OVERRIDDEN): {validator}",
                            ),
                        )
                    else:
                        self.stdout.write(f"  Updated validator: {validator}")

                # Sync signal definitions and derivations from the
                # validator config's catalog_entries spec.
                seen_signal_keys: set[tuple[str, str]] = set()
                seen_derivation_keys: set[str] = set()

                for entry in cfg.catalog_entries:
                    entry_data = entry.model_dump()
                    entry_slug = entry_data.pop("slug")
                    entry_type = entry_data.pop("entry_type")

                    if entry_type == "derivation":
                        Derivation.objects.update_or_create(
                            validator=validator,
                            contract_key=entry_slug,
                            defaults={
                                "expression": entry.binding_config.get(
                                    "expr",
                                    "",
                                ),
                                "data_type": entry.data_type,
                                "order": entry.order,
                            },
                        )
                        seen_derivation_keys.add(entry_slug)
                        total_derivations_synced += 1
                    elif entry_type == "signal":
                        provider_binding = build_provider_binding_from_mapping(
                            entry.binding_config,
                        )
                        SignalDefinition.objects.update_or_create(
                            validator=validator,
                            contract_key=entry_slug,
                            direction=entry.run_stage,
                            defaults={
                                "native_name": entry_slug,
                                "label": entry.label or "",
                                "description": entry.description or "",
                                "data_type": entry.data_type,
                                "order": entry.order,
                                "unit": (entry.metadata or {}).get("units", ""),
                                "origin_kind": SignalOriginKind.CATALOG,
                                "source_kind": entry.source_kind,
                                "is_path_editable": entry.is_path_editable,
                                "provider_binding": provider_binding,
                                "metadata": entry.metadata,
                            },
                        )
                        seen_signal_keys.add((entry_slug, entry.run_stage))
                        total_signals_synced += 1

                # Prune signals/derivations that are no longer declared
                # in the config (e.g., renamed or removed entries). Only
                # prune CATALOG-origin signals — step-owned signals
                # (FMU, template) are managed separately.
                if cfg.catalog_entries:
                    pruned_sigs = SignalDefinition.objects.filter(
                        validator=validator,
                        origin_kind=SignalOriginKind.CATALOG,
                    )
                    for key, direction in seen_signal_keys:
                        pruned_sigs = pruned_sigs.exclude(
                            contract_key=key,
                            direction=direction,
                        )
                    pruned_count = pruned_sigs.count()
                    if pruned_count:
                        pruned_sigs.delete()
                        self.stdout.write(
                            f"  Pruned {pruned_count} stale signal(s)",
                        )

                    pruned_derivs = Derivation.objects.filter(
                        validator=validator,
                    ).exclude(contract_key__in=seen_derivation_keys)
                    pruned_d_count = pruned_derivs.count()
                    if pruned_d_count:
                        pruned_derivs.delete()
                        self.stdout.write(
                            f"  Pruned {pruned_d_count} stale derivation(s)",
                        )

                    self.stdout.write(
                        f"  Signals: {total_signals_synced} synced, "
                        f"derivations: {total_derivations_synced} synced",
                    )

                # NOTE: We do NOT call ensure_step_signal_bindings() here for
                # existing steps using this validator. This command runs on
                # startup/deploy and iterating all steps would be expensive.
                # Instead, ensure_step_signal_bindings() handles binding
                # creation at step creation/update time (in save_workflow_step).
                # For backfilling existing steps, use a one-off data migration.

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Sync complete: "
                f"{total_validators_created} validators created, "
                f"{total_validators_updated} updated. "
                f"{total_signals_synced} signals synced, "
                f"{total_derivations_synced} derivations synced."
            ),
        )
