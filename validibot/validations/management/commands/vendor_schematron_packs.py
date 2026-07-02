"""Materialise vendored Schematron packs into library validators.

Implements the ADR-2026-07-01 D5 vendoring flow: ``packs.py`` is the
code-reviewed allowlist of curated rule packs; this command projects it into
the database, creating for each pack —

1. the **global pack ``Ruleset`` row** (``org=None``, metadata carrying the
   full descriptor + pinned checksums, validated by ``Ruleset.clean()``), and
2. the **library ``Validator`` row** (``org=None``, ``is_system=True``) whose
   ``default_ruleset`` is that pack row, sharing the one Schematron engine
   config — the same library pattern custom SHACL validators use, so packs
   appear as first-class validators in the library and the step wizard.

Lifecycle rules (D5): pins are immutable. Re-running the command is
idempotent; a *new* pack version produces a *new* validator row (bumped
integer revision) with a *new* pack ruleset row — existing rows are never
mutated, so steps pinned by FK keep resolving the exact bytes they were
authored against. A checkout whose artefact bytes drift from the registry
pin fails the whole command before any row is written.

Pack validator rows deliberately set ``config_provider=""`` so
``sync_validators``'s missing-config sweep (which only inspects
config-managed rows) never marks them unavailable — they are library rows
owned by this command, not by config discovery.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction
from django.db.models import Max

from validibot.validations.constants import RulesetType
from validibot.validations.constants import SignalOriginKind
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.models import Ruleset
from validibot.validations.models import StepIODefinition
from validibot.validations.models import Validator
from validibot.validations.services.catalog_entry_normalization import (
    build_provider_binding_from_mapping,
)
from validibot.validations.validators.base.config import get_config
from validibot.validations.validators.schematron.packs import SchematronPack
from validibot.validations.validators.schematron.packs import (
    SchematronPackResolutionError,
)
from validibot.validations.validators.schematron.packs import get_pack
from validibot.validations.validators.schematron.packs import list_packs
from validibot.validations.validators.schematron.staging import (
    verified_pack_artifact_path,
)

# Config fields that have no column on the Validator model (mirrors the
# exclusion set sync_validators uses when projecting a ValidatorConfig).
_CONFIG_FIELDS_WITHOUT_COLUMNS = {
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
    "step_serializer_class",
    "validator_class",
}


class Command(BaseCommand):
    help = (
        "Materialise the vetted Schematron packs from packs.py into "
        "library Validator rows + global pack Ruleset rows (ADR-2026-07-01)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--pack",
            action="append",
            dest="packs",
            metavar="ID@VERSION",
            help=(
                "Vendor only this pack (repeatable). Defaults to every "
                "pack registered in packs.py, including deprecated ones "
                "(their rows must keep existing for pinned steps)."
            ),
        )

    def handle(self, *args, **options):
        packs = self._selected_packs(options.get("packs"))
        if not packs:
            self.stdout.write(
                self.style.WARNING(
                    "No Schematron packs are registered in packs.py — "
                    "nothing to vendor.",
                ),
            )
            return

        engine_config = get_config(ValidationType.SCHEMATRON)
        if engine_config is None:
            msg = (
                "No SCHEMATRON ValidatorConfig is registered — cannot "
                "vendor packs without the engine config."
            )
            raise CommandError(msg)

        for pack in packs:
            self._vendor_pack(pack, engine_config)

        self.stdout.write(self.style.SUCCESS(f"Vendored {len(packs)} pack(s)."))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _selected_packs(self, selections: list[str] | None) -> list[SchematronPack]:
        """Resolve --pack selections (or all registered packs)."""
        if not selections:
            return list_packs(include_deprecated=True)

        packs: list[SchematronPack] = []
        for selection in selections:
            pack_id, sep, pack_version = selection.partition("@")
            pack = get_pack(pack_id, pack_version) if sep else None
            if pack is None:
                msg = (
                    f"Pack '{selection}' is not registered in packs.py "
                    f"(expected ID@VERSION of a vetted pack)."
                )
                raise CommandError(msg)
            packs.append(pack)
        return packs

    @transaction.atomic
    def _vendor_pack(self, pack: SchematronPack, engine_config) -> None:
        """Create-or-verify the pack ruleset + library validator rows."""
        # 1. Verify the artefact bytes against the pin BEFORE touching the
        # DB — a drifted checkout must not materialise anything.
        try:
            verified_pack_artifact_path(pack)
        except SchematronPackResolutionError as exc:
            raise CommandError(str(exc)) from exc

        ruleset = self._ensure_pack_ruleset(pack)
        validator = self._ensure_pack_validator(pack, ruleset, engine_config)
        self._sync_catalog_signals(validator, engine_config)

    def _ensure_pack_ruleset(self, pack: SchematronPack) -> Ruleset:
        """Create the global pack ruleset row, or verify the existing pin.

        Existing rows are never mutated (D5 immutability): if the stored
        pins disagree with the registry, that's a drifted vendoring and the
        command refuses rather than rewriting what pinned steps resolve.
        """
        descriptor = {
            "pack_id": pack.id,
            "pack_version": pack.version,
            "pack_source_sha256": pack.source_sha256,
            "pack_artifact_sha256": pack.artifact_sha256,
            "syntax": pack.syntax,
            "source_url": pack.source_url,
            "license": pack.license,
            "query_binding": pack.query_binding,
            "engine": pack.engine,
            "artifact": pack.artifact,
            "rule_doc_url_template": pack.rule_doc_url_template,
        }
        if pack.deprecated:
            descriptor["deprecated"] = True
            descriptor["superseded_by"] = pack.superseded_by

        existing = Ruleset.objects.filter(
            org=None,
            ruleset_type=RulesetType.SCHEMATRON,
            name=pack.id,
            version=pack.version,
        ).first()
        if existing is not None:
            stored_sha = (existing.metadata or {}).get("pack_artifact_sha256")
            if stored_sha != pack.artifact_sha256:
                msg = (
                    f"Pack ruleset for {pack.id}@{pack.version} already "
                    f"exists with artifact sha {str(stored_sha)[:12]}… but "
                    f"the registry pins {pack.artifact_sha256[:12]}… — "
                    f"pinned versions are immutable; register a new pack "
                    f"version instead."
                )
                raise CommandError(msg)
            self.stdout.write(f"  Pack ruleset exists: {pack.id}@{pack.version}")
            return existing

        ruleset = Ruleset(
            org=None,
            user=None,
            name=pack.id,
            ruleset_type=RulesetType.SCHEMATRON,
            version=pack.version,
            metadata=descriptor,
        )
        ruleset.full_clean()
        ruleset.save()
        self.stdout.write(
            self.style.SUCCESS(f"  Created pack ruleset: {pack.id}@{pack.version}"),
        )
        return ruleset

    def _ensure_pack_validator(
        self,
        pack: SchematronPack,
        ruleset: Ruleset,
        engine_config,
    ) -> Validator:
        """Create the library validator row for this pack version.

        Row identity IS the pin (D5): steps reference concrete rows by FK,
        so an existing row for this pack ruleset is left untouched apart
        from display/lifecycle fields. A new pack version gets a new row
        with the next integer revision for the slug.
        """
        slug = f"schematron-{pack.id}"

        validator = Validator.objects.filter(
            slug=slug,
            default_ruleset=ruleset,
        ).first()
        if validator is not None:
            self.stdout.write(f"  Pack validator exists: {validator}")
            return validator

        engine_fields = engine_config.model_dump(
            exclude=_CONFIG_FIELDS_WITHOUT_COLUMNS,
        )
        engine_fields.update(
            {
                "slug": slug,
                "name": f"{pack.title} ({pack.version})",
                "short_description": (
                    f"Curated Schematron rule pack {pack.id}@{pack.version}. "
                    f"{pack.title} — runs the publisher's rules and reports "
                    f"failures by their native IDs."
                ),
                "description": (
                    f"Runs the curated, version-pinned Schematron rule pack "
                    f"'{pack.title}' (id: {pack.id}, version: "
                    f"{pack.version}) against XML submissions in an "
                    f"isolated container. Source: {pack.source_url} "
                    f"(license: {pack.license}). Findings carry the "
                    f"publisher's native rule identifiers. This is a "
                    f"pre-flight developer aid, not a certification of "
                    f"compliance."
                ),
                "org": None,
                "is_system": True,
                # Library row owned by THIS command — an empty provider
                # exempts it from sync_validators' missing-config sweep.
                "config_provider": "",
                "availability_state": ValidatorAvailabilityState.AVAILABLE,
                "availability_message": "",
            },
        )
        next_version = (
            Validator.objects.filter(slug=slug).aggregate(Max("version"))[
                "version__max"
            ]
            or 0
        ) + 1
        engine_fields["version"] = next_version

        validator = Validator.objects.create(
            default_ruleset=ruleset,
            **engine_fields,
        )
        self.stdout.write(
            self.style.SUCCESS(f"  Created pack validator: {validator}"),
        )
        return validator

    def _sync_catalog_signals(self, validator: Validator, engine_config) -> None:
        """Sync the engine's ``o.*`` catalog onto this pack validator row.

        Signal machinery reads ``StepIODefinition`` rows per validator row,
        so every pack validator needs its own copy of the engine catalog.
        Mirrors the signal branch of ``sync_validators`` (the Schematron
        config declares SIGNAL entries only, so no derivation handling).
        """
        synced = 0
        for entry in engine_config.catalog_entries:
            provider_binding = build_provider_binding_from_mapping(
                entry.binding_config,
            )
            StepIODefinition.objects.update_or_create(
                validator=validator,
                contract_key=entry.slug,
                direction=entry.run_stage,
                defaults={
                    "native_name": entry.slug,
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
                    "on_missing": entry.on_missing,
                },
            )
            synced += 1
        self.stdout.write(f"  Synced {synced} catalog signal(s)")
