"""Schematron validator package (ADR-2026-07-01).

Executes curated, version-pinned Schematron rule packs (EN 16931, Peppol BIS
Billing 3.0, …) against XML submissions in an isolated container backend, and
maps the resulting SVRL report into findings that preserve the publishers'
native rule identifiers.

Modules:

- ``config.py`` — the ``ValidatorConfig`` (auto-discovered by
  ``discover_configs()``; creating that module is the whole registration).
- ``validator.py`` — ``SchematronValidator`` (an ``AdvancedValidator``).
- ``packs.py`` — the curated, checksummed rule-pack allowlist + resolvers.
- ``staging.py`` — checksum-verified pack-artefact staging (both dispatchers).
- ``svrl.py`` — re-export of the canonical SVRL parser in
  ``validibot_shared.schematron.svrl`` (shared >= 0.11.0).
- ``security.py`` — hardened-XML guard + the D8 resource-limit table.
- ``launch.py`` — resolves ``SchematronInputs`` for the container envelope
  (requires ``validibot-shared`` >= 0.11.0; imported only at dispatch time).
"""

from validibot.validations.validators.schematron.validator import SchematronValidator

__all__ = ["SchematronValidator"]
