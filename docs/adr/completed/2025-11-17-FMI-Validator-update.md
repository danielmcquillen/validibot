# ADR Update: FMI Validator Implementation Progress

**Status:** In Progress (2025-11-19 implementation notes)  
**Original ADR:** [2025-11-17-FMI-Validator](2025-11-17-FMI-Validator.md)

## Summary of work completed

- Introduced models to store FMUs, parsed variables, and probe status (`FMUModel`, `FMIVariable`, `FMUProbeResult`) plus catalog flags (`is_hidden`, `default_value`) to align with the ADR’s “catalog-driven IO” requirement.
- Added an FMI validator type, engine, and provider wiring so workflow steps can select FMI alongside existing validators. FMI validators enforce FMU presence for org-owned entries and expose catalog entries derived from the FMU metadata.
- Implemented FMU upload/introspection service:
  - Uploads the FMU (supports pluggable storage), reads `modelDescription.xml`, captures FMU metadata, and seeds `FMIVariable` rows.
  - Seeds validator catalog entries for inputs/outputs with stable slugs and type/unit metadata.
  - Records an initial probe status row to track approval.
- Implemented probe flow:
  - Modal `probe_fmu` function (in `sv_modal_dev/sv_fmi/modal_app.py`) validates the FMU archive, parses `ScalarVariable` entries, and returns `FMIProbeResult` using shared Pydantic models (`vb_shared_dev/vb_shared/fmi`).
  - Django service `run_fmu_probe` invokes the Modal probe, updates `FMUProbeResult`, refreshes variables/catalog entries, and marks FMUs approved on success.
  - Added Celery task `run_fmu_probe_task` and HTMX endpoints/buttons to trigger and poll probe status from the validator detail page.
- Upgraded Modal “run” path:
  - `sv_fmi.modal_app.run_fmi_simulation` now uses `fmpy` to run short simulations and returns structured outputs; still constrained to short horizons to keep the runner safe and fast.
- UI and authoring updates:
  - Validator Library now offers “New FMI Validator” with an FMU upload form and probe status panel on validator detail.
  - Workflow authoring guide documents FMI usage and clarifies “probe” as a short safety/metadata run before assertions are allowed.
- Test assets:
  - Added a MIT-licensed reference FMU (`Feedthrough.fmu`) under `tests/assets/fmu/` to exercise probes and modal runs in tests.
- Tests:
  - Added Django `TestCase` suites for FMI services (creation, probe refresh), engine dispatch, and probe HTMX endpoints; all green.

## How this aligns with the ADR goals

- **Secure execution off the web stack:** FMUs are stored as assets; all probe/simulation runs go through Modal functions (`fmi-runner`), keeping Django stateless with respect to execution.
- **Automatic IO discovery:** Parsing `modelDescription.xml` populates `FMIVariable` rows and validator catalog entries (inputs/outputs), fulfilling the “automatic introspection to catalog” decision.
- **Approval/probe flow:** Probe status tracking and a callable probe runner implement the ADR’s safety gate before FMUs are approved for workflow use.
- **Catalog-driven assertions:** FMI validators publish catalog entries with slugs, types, and units; CEL assertions run against outputs returned from Modal.
- **UI authoring:** New FMI creation page in the Validator Library with probe controls keeps authors on the rails and matches the “wizard” intent, while we continue to harden end-to-end execution.

## Remaining gaps / next steps

1. **End-to-end binding UI:** Add bindings in the workflow step editor so authors can map submission fields/signals to FMU inputs and reference outputs in CEL assertions.
2. **Probe UX polish:** HTMX polling works; add inline progress indicators and error surfaces on the validator detail page.
3. **Runtime hardening:** Enforce resource/time limits in Modal, add storage download (S3/local) abstraction, and surface execution logs more richly.
4. **E2E tests:** Add full workflow tests that run FMI steps with mocked Modal, plus integration tests once Modal is available in CI.
5. **Docs:** Expand operator-facing docs with examples of binding inputs/outputs, probe expectations, supported FMI versions/kinds, and failure modes.

## Context for Validibot goals

This work turns the FMI ADR from a concept into a usable preview: authors can upload FMUs, inspect discovered inputs/outputs, run probes safely, and (via Modal) execute short simulations without touching core app servers. It keeps the platform aligned with the larger goal of supporting simulation-backed validations while maintaining isolation, observability, and catalog-driven authoring. As we finish bindings and harden execution, FMI steps will become first-class citizens alongside JSON Schema and EnergyPlus.
