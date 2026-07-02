"""Schematron validator package (ADR-2026-07-01).

Runs author-uploaded Schematron rules (e.g. a published standard's official
``.sch`` file — EN 16931, Peppol BIS Billing 3.0, …) against XML submissions
in an isolated container backend, and maps the resulting SVRL report into
findings that preserve the rules' native identifiers. The rules are stored
on the step's Ruleset (like an XSD or SHACL shapes) and travel inline in
the container envelope; compiled Schematron is executable XSLT, so
execution only ever happens in the sandbox.

Modules:

- ``config.py`` — the ``ValidatorConfig`` (auto-discovered by
  ``discover_configs()``; creating that module is the whole registration).
- ``validator.py`` — ``SchematronValidator`` (an ``AdvancedValidator``).
- ``svrl.py`` — re-export of the canonical SVRL parser in
  ``validibot_shared.schematron.svrl``.
- ``security.py`` — hardened-XML guards (submission + uploaded rules) and
  the D8 resource-limit table.
- ``launch.py`` — resolves ``SchematronInputs`` (inline rules text) for the
  container envelope (requires ``validibot-shared`` >= 0.12.0; imported
  only at dispatch time).
"""

from validibot.validations.validators.schematron.validator import SchematronValidator

__all__ = ["SchematronValidator"]
