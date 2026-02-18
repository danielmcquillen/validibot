# Validators

Validators define the concrete execution engine that a workflow step will call. One workflow step uses
one validator. Validators bundle the technical contract (validation type + provider version), the catalog
of signals/derivations the engine exposes, and optional organization-specific extensions (custom validators).
The platform now ships with five stock validation types:

- **BASIC** — no provider backing; authors add assertions manually via the UI. These steps still produce
  rulesets so findings remain auditable, but there is no catalog beyond whatever the assertion references.
- **JSON_SCHEMA / XML_SCHEMA** — schema validations that require uploading or pasting schema content.
- **ENERGYPLUS** — advanced simulation validators with IDF/simulation options and catalog entries.
- **AI_ASSIST** — template-driven AI validations (policy check, critic, etc.).
- **CUSTOM_VALIDATOR** — organization-defined validators registered via the custom validator UI (displayed as “Custom Basic Validator”).

**Terminology note:** `ValidationType` (the enum) describes the _kind of validation_ being performed
(BASIC, JSON_SCHEMA, ENERGYPLUS, etc.), not the validator itself. Multiple `Validator` rows can share
the same `ValidationType` -- for example, several custom validators all use `CUSTOM_VALIDATOR`. Think
of it as "validation engine type" rather than "validator type."

Validators are stored in the `validators` table. Each row records the following:

- `slug`, `name`, `description`, `validation_type`, `version`
- relationship to an organization (`org_id`) and `is_system` flag
- timestamp fields plus the related `custom_validator` entry when the row was created by an org

Every workflow step references a validator row. During execution the validator tells the runtime which
provider class to load, which helper functions are legal, and how to interpret rule and assertion catalogs.

## Catalog entries (signals, outputs, derivations)

Validators own the canonical catalog describing what the validation engine can read or emit. Catalog
rows live in `validator_catalog_entries` and carry:

| Field            | Meaning                                                                                                                                  |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `entry_type`     | `signal` or `derivation`.                                                                                                                |
| `run_stage`      | Whether the entry is available during the **input** phase (before the engine runs) or the **output** phase (after the engine completes). |
| `slug`           | Stable identifier referenced by rulesets and assertions.                                                                                 |
| `data_type`      | Scalar/list metadata (number, datetime, bool, series).                                                                                   |
| `binding_config` | Provider-specific hints (e.g., EnergyPlus meter path).                                                                                   |
| `metadata`       | Free-form JSON used by the UI and provider tooling.                                                                                      |

Inputs represent values already available before the engine runs (project metadata, uploaded files,
environment). Outputs represent telemetry the engine emits during execution. Derivations describe
computed metrics, and their `run_stage` flag indicates whether they enrich inputs or post-process
validator outputs. By centralising these definitions on the validator we let every ruleset reuse them without duplicating structure inside each default assertion. Workflow step authors can still define as many assertions as necessary by referencing the catalog slugs stored on the validator; see [Ruleset Assertions](assertions.md) for how those references are persisted and executed.
Basic validators intentionally skip catalog management; every assertion directly references the custom
target path the author entered.

## Validator default assertions (vs. workflow assertions)

- **Default assertions**: logic defined on a validator itself (for example, default CEL expressions). Stored on `validator_catalog_rules` and can reference one or more catalog entries via `validator_catalog_rule_entries`. Default assertions are evaluated according to the validator’s engine and ordering; deleting a default assertion cleans up its links.
- **Step assertions**: logic defined on workflow steps against a ruleset. Stored separately and evaluated in workflow runs. Assertions are the only logic workflow authors manage today.

Catalog entries cannot be deleted while referenced by default assertions; default assertions can be deleted at any time (links are removed).

## Custom validators

Custom validators give organizations their own catalog on top of a base validation type. They live in
the `custom_validators` table, linked back to a standard validator row. Authors can select a base type
(initially Modelica or PyWinCalc) and then define:

1. Name, description, notes, and `custom_type`.
2. All catalog entries (signals, derivations, helper metadata) that the validator should expose.
3. Optional helper settings (instrumentation policy, provider config).

When saved, the system persists a new `validators` row plus any catalog entries the author provided.
Rulesets that pick this custom validator automatically see the custom catalog, and the validator
detail page shows who owns and maintains it. Custom validators stay scoped to the org that created
them; system validators remain read-only.

Catalog changes are versioned on the validator. Editing a custom validator updates the catalog for all
rulesets referencing it, so catalog slugs stay globally unique per validator.

## Resource files

Advanced validators may require auxiliary files to run (e.g., EnergyPlus needs EPW weather files,
FMI validators need shared libraries). These are stored as `ValidatorResourceFile` rows linked to
a validator via FK.

Each resource file has:

- **Scoping**: `org=NULL` for system-wide resources visible to all orgs, or `org=<org>` for
  org-specific files. System-wide files are only manageable by superusers.
- **Type**: `resource_type` from the `ResourceFileType` enum (currently `ENERGYPLUS_WEATHER`).
- **Validation**: Each type maps to a `ResourceTypeConfig` in `validations/constants.py` that
  defines allowed extensions, max file size, and optional header validation. Adding a new resource
  type requires only adding a config entry -- no form or view changes needed.

Resource files are referenced by step configs via UUID strings in a JSONField
(`resource_file_ids: list[str]` in the Pydantic step config). This is a deliberate design choice
rather than FKs or M2M, keeping `step.config` as the single source of truth and avoiding
dual-write complexity. See the Validator Resource File RBAC ADR in `validibot-project` for the full rationale.

**RBAC**: Authors can view and select resource files, but only ADMIN/OWNER can create, edit, or
delete them (uses `ADMIN_MANAGE_ORG` permission). Deletion is blocked if the file is referenced
by any active workflow step.

## Validator detail page (UI)

The validator detail page uses link-based tabs (separate URLs, server-rendered) rather than
JavaScript tabs. The tab layout is:

| Tab                    | URL pattern                             | View                            | Default |
| ---------------------- | --------------------------------------- | ------------------------------- | ------- |
| **Description**        | `library/custom/<slug>/`                | `ValidatorDetailView`           | Yes     |
| **Signals**            | `library/custom/<slug>/signals-tab/`    | `ValidatorSignalsTabView`       |         |
| **Default Assertions** | `library/custom/<slug>/assertions/`     | `ValidatorAssertionsTabView`    |         |
| **Resource Files**     | `library/custom/<slug>/resource-files/` | `ValidatorResourceFilesTabView` |         |

All tabs share the same base template (`validator_detail.html`) and render their content
conditionally via `active_tab`. Each tab includes only its own modals to reduce page weight.

## Provider resolution

The runtime resolves a provider implementation for every validator. Providers are in-process classes
registered per `(validation_type, semantic version range)` pair. Functions such as
`BaseValidatorEngine.resolve_provider()` call the registry and cache the matching provider instance.

Providers must implement the following contract:

- `json_schema()` — optional schema for provider config blocks.
- `catalog_entries(validator)` — canonical catalog rows for the validator (built-in validators read
  bundled definitions; custom validators read DB-backed rows).
- `cel_functions()` — custom helper metadata appended to the default helper set.
- `preflight_validate(ruleset, merged_catalog)` — domain specific validation before a ruleset is accepted.
- `instrument(model_copy, ruleset)` — optional adjustments to the uploaded artifact (e.g., inject EnergyPlus output objects).
- `bind(run_ctx, merged_catalog)` — builds per-run bindings so CEL helpers (e.g., `series('meter')`) can resolve data on demand.

The provider gives the validator its domain-specific abilities without storing Python dotted paths in
the database. Version upgrades happen entirely inside code by registering new providers ranges.

## Advanced validator seed data

Advanced validators (EnergyPlus, FMI, etc.) are packaged as Docker containers and have predefined
input/output signals. These signals are defined as **seed data** in `validibot/validations/seeds/`
and synced to the database at startup.

The seed data for each advanced validator includes:

1. **Validator metadata** — slug, name, description, validation type, and configuration.
2. **Input signals** — parameters available before the validator runs (e.g., `expected_floor_area_m2`
   for EnergyPlus). These enable "Input Assertions" in the step editor.
3. **Output signals** — metrics produced by the validator (e.g., `site_electricity_kwh` for EnergyPlus).
   These enable "Output Assertions" in the step editor.
4. **Derivations** — computed metrics derived from signals (e.g., `total_unmet_hours` calculated from
   heating and cooling unmet hours).

The seed data binding configurations must match field names in the corresponding shared library models
(e.g., `validibot_shared.energyplus.models.EnergyPlusSimulationMetrics`), which is what the container
validator populates after running the simulation.

To sync seed data to the database:

```bash
python manage.py sync_advanced_validators
```

This command is idempotent and runs automatically at container startup. It creates validators and
catalog entries if they don't exist, or updates them if the seed data has changed.

**Versioning (current approach):** The validator `version` field in seed data tracks the overall
validator version. When signals change significantly, bump this version. The sync command updates
existing validators but uses `get_or_create` for catalog entries (existing entries are preserved).
For now, if you need to change existing catalog entries, manually update them in the database or
delete and re-sync. A more sophisticated versioning system is planned for the future
([GitHub issue #92](https://github.com/danielmcquillen/validibot/issues/92)).

## Validator lifecycle

1. **Registration** — migrations or bootstrap logic call `create_default_validators()` to ensure every
   built-in validator row exists. Advanced validators are synced via `sync_advanced_validators` which
   populates their catalog entries. Custom validators are created through the Validator Library UI.
2. **Selection** — workflow steps reference a validator via FK. When the step runs, the engine fetches
   the validator, resolves its provider, and loads the catalog/allowlists.
3. **Ruleset preparation** — when authors publish a ruleset against a validator, the preparation
   service ensures every referenced slug exists in the validator catalog and that the helper functions
   used in derivations/assertions are allowed. Prepared plans are cached by validator + provider version.
4. **Execution** — the provider optionally instruments the uploaded artifact, binds helper closures,
   and the engine evaluates derivations followed by assertions. Findings are emitted with references
   back to the validator + catalog snapshot for auditability.

See [Assertions](assertions.md) for how the validator catalog is consumed by rulesets, and
how findings reference these slugs.
