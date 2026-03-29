# Validators

Validators define the concrete validation class that a workflow step will call. One workflow step uses
one validator. Validators bundle the technical contract (validation type + provider version), the catalog
of signals/derivations the validator exposes, and optional organization-specific extensions (custom validators).
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
of it as "validation class type" rather than "validator type."

Validators are stored in the `validators` table. Each row records the following:

- `slug`, `name`, `description`, `validation_type`, `version`
- relationship to an organization (`org_id`) and `is_system` flag
- timestamp fields plus the related `custom_validator` entry when the row was created by an org

Every workflow step references a validator row. During execution the validator tells the runtime which
provider class to load, which helper functions are legal, and how to interpret rule and assertion catalogs.

## Catalog entries (signals, outputs, derivations)

Validators own the canonical catalog describing what the validator can read or emit. Catalog
rows live in `validator_catalog_entries` and carry:

| Field            | Meaning                                                                                                                                  |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `entry_type`     | `signal` or `derivation`.                                                                                                                |
| `run_stage`      | Whether the entry is available during the **input** phase (before the validator runs) or the **output** phase (after the validator completes). |
| `slug`           | Stable identifier referenced by rulesets and assertions.                                                                                 |
| `data_type`      | Scalar/list metadata (number, datetime, bool, series).                                                                                   |
| `binding_config` | Provider-specific hints (e.g., EnergyPlus meter path).                                                                                   |
| `metadata`       | Free-form JSON used by the UI and provider tooling.                                                                                      |

Inputs represent values already available before the validator runs (project metadata, uploaded files,
environment). Outputs represent telemetry the validator emits during execution. Derivations describe
computed metrics, and their `run_stage` flag indicates whether they enrich inputs or post-process
validator outputs. By centralising these definitions on the validator we let every ruleset reuse them without duplicating structure inside each default assertion. Workflow step authors can still define as many assertions as necessary by referencing the catalog slugs stored on the validator; see [Ruleset Assertions](assertions.md) for how those references are persisted and executed.
Basic validators intentionally skip catalog management; every assertion directly references the custom
target path the author entered.

## Validator default assertions (vs. workflow assertions)

- **Default assertions**: logic defined on a validator itself (for example, default CEL expressions). Stored on `validator_catalog_rules` and can reference one or more catalog entries via `validator_catalog_rule_entries`. Default assertions are evaluated according to the validator’s ordering; deleting a default assertion cleans up its links.
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
FMU validators need shared libraries). These are stored as `ValidatorResourceFile` rows linked to
a validator via FK.

Each resource file has:

- **Scoping**: `org=NULL` for system-wide resources visible to all orgs, or `org=<org>` for
  org-specific files. System-wide files are only manageable by superusers.
- **Type**: `resource_type` from the `ResourceFileType` enum (currently `ENERGYPLUS_WEATHER`).
- **Validation**: Each type maps to a `ResourceTypeConfig` in `validations/constants.py` that
  defines allowed extensions, max file size, and optional header validation. Adding a new resource
  type requires only adding a config entry -- no form or view changes needed.

Resource files are referenced by workflow steps via the `WorkflowStepResource` through table
(FK-backed). Each step resource has a `role` (e.g., `WEATHER_FILE`, `MODEL_TEMPLATE`) and
points to either a shared `ValidatorResourceFile` (catalog reference, PROTECT on delete) or
stores its own file directly (step-owned, CASCADE with step). This provides referential
integrity and eliminates the stale-UUID problem of the earlier JSON-based approach.

**RBAC**: Authors can view and select resource files, but only ADMIN/OWNER can create, edit, or
delete them (uses `ADMIN_MANAGE_ORG` permission). Deletion is blocked if the file is referenced
by any active workflow step (checked via `WorkflowStepResource` FK query).

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
`BaseValidator.resolve_provider()` call the registry and cache the matching provider instance.

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

## Validator configuration

`ValidatorConfig` (a Pydantic model in `validations/validators/base/config.py`) is the **single
source of truth** for each system validator. Everything the platform needs to know about a
validator is declared in one place:

- **Identity and DB sync** — `slug`, `name`, `description`, `validation_type`, `version`, `order`.
  The `sync_validators` management command reads these fields and creates or updates `Validator`
  rows and their catalog entries in the database.
- **Validator class binding** — `validator_class` is a dotted Python path to the `BaseValidator`
  subclass (e.g., `"validibot.validations.validators.energyplus.validator.EnergyPlusValidator"`).
  At startup, `register_validators()` resolves this path and stores the class in the runtime
  registry so the engine can instantiate validators without storing Python paths in the database.
- **File handling** — `supported_file_types`, `supported_data_formats`, `allowed_extensions`,
  and `resource_types` declare what files the validator accepts.
- **Compute** — `compute_tier` (LOW, MEDIUM, HIGH) tells the platform how much resource the
  validator needs when dispatching containers.
- **Display** — `icon` (Bootstrap Icons class) and `card_image` for the validator library UI.
- **Catalog entries** — a list of `CatalogEntrySpec` objects describing signals, outputs, and
  derivations the validator exposes. These map 1:1 to `ValidatorCatalogEntry` rows in the database.
- **Step editor cards** — a list of `StepEditorCardSpec` objects that inject custom UI cards
  into the workflow step detail page (see below).

### Where configs live

Validators that are full sub-packages (EnergyPlus, FMU, etc.) declare their config in a
`config.py` module inside the package. For example, the EnergyPlus config lives at
`validibot/validations/validators/energyplus/config.py` and exports a module-level `config`
attribute.

Every validator is a sub-package under `validations/validators/` with its own `config.py`
declaring a `ValidatorConfig` instance.

### Discovery and registry population

At startup, `register_validators()` runs inside `ValidationsConfig.ready()` and does a single
pass over all validator configs. It calls `discover_configs()`, which walks the
`validations/validators/` directory and imports any sub-package that has a `config.py` with a
`ValidatorConfig` instance.

For each config, two registries are populated:

- The **config registry** — keyed by `validation_type`, stores the `ValidatorConfig` for metadata
  lookups (catalog entries, file types, display info, step editor cards).
- The **validator class registry** — keyed by `validation_type`, stores the resolved Python class
  for runtime instantiation. Only populated if the config declares a `validator_class` path.

This unified approach replaced an earlier two-registry system where configs and classes were
registered separately via different mechanisms.

### Syncing to the database

```bash
python manage.py sync_validators
```

This command reads from the config registry and creates or updates `Validator` rows and their
catalog entries. It is idempotent and runs automatically at container startup.

**Versioning (current approach):** The `version` field tracks the overall validator version. When
signals change significantly, bump this version. The sync command updates existing validators but
uses `get_or_create` for catalog entries (existing entries are preserved). For now, if you need
to change existing catalog entries, manually update them in the database or delete and re-sync.
A more sophisticated versioning system is planned for the future
([GitHub issue #92](https://github.com/danielmcquillen/validibot/issues/92)).

### Step editor cards

Validators can declare custom UI cards that appear in the workflow step detail page's right
column via `StepEditorCardSpec` objects in the config's `step_editor_cards` list. This extension
point is available for future use, but no validators currently declare custom cards.

!!! note "Template variables use the unified signals card"
    Since ADR-2026-03-10, template variable editing is handled by the unified "Inputs and
    Outputs" card that appears on every step detail page. Template variables are treated as
    input signals with `source="template"`, alongside catalog entries with `source="catalog"`.
    Each template variable has a per-variable edit modal for annotations (label, default,
    type, constraints). This replaced the earlier `StepEditorCardSpec`-based approach.

Each card spec has the following fields:

- `slug` — unique identifier, used for the HTML `id` attribute and HTMx targeting.
- `label` — display text shown in the card header.
- `template_name` — Django template path to render the card content.
- `form_class` — optional dotted path to a Form class. If provided, the card renders an
  editable form. Resolved via `import_string()`.
- `view_class` — optional dotted path to a View class that handles GET/POST for the card.
  If omitted, the card is rendered inline with no separate endpoint.
- `order` — position within the right column (lower numbers appear higher).
- `condition` — optional dotted path to a `func(step) -> bool` callable. When set, the card
  only renders if the function returns `True`.

### Unified signals card

Every step detail page shows an "Inputs and Outputs" card in the right column. This card
merges two sources of signals into a unified view:

- **Catalog entries** — defined in the validator's `ValidatorConfig.catalog_entries` and synced
  to the database. These represent signals the validator produces (outputs) or consumes
  (inputs). Source badge: "Catalog".
- **Template variables** — discovered from uploaded template files (e.g. `$U_FACTOR` in an
  EnergyPlus IDF). Stored in `step.config["template_variables"]`. Source badge: "Template".

The card has two tabs when both input and output signals exist:

- **Input Signals** — catalog INPUT entries + template variables, merged in order.
  Template-source signals have an Edit button (pencil icon) that opens a per-variable
  annotation modal (`SingleTemplateVariableForm`).
- **Output Signals** — catalog OUTPUT entries, each with a "show to user" indicator
  based on the step's `display_signals` config.

The `build_unified_signals()` helper in `views_helpers.py` builds this merged representation
at the view layer. No database model changes are needed — it's purely a presentation concern.

### Concrete example: EnergyPlus config

Here's a condensed look at the EnergyPlus config (`validators/energyplus/config.py`) to show
how all these pieces fit together:

```python
from validibot.validations.validators.base.config import (
    CatalogEntrySpec,
    ValidatorConfig,
)

config = ValidatorConfig(
    slug="energyplus-idf-validator",
    name="EnergyPlus Validator",
    validation_type=ValidationType.ENERGYPLUS,
    validator_class=(
        "validibot.validations.validators.energyplus"
        ".validator.EnergyPlusValidator"
    ),
    compute_tier=ComputeTier.HIGH,
    supports_assertions=True,
    catalog_entries=[
        CatalogEntrySpec(
            slug="site_electricity_kwh",
            label="Site Electricity (kWh)",
            entry_type="signal",
            run_stage="output",
            data_type="number",
            binding_config={"source": "metric", "key": "site_electricity_kwh"},
        ),
        CatalogEntrySpec(
            slug="total_unmet_hours",
            label="Total Unmet Hours",
            entry_type="derivation",
            run_stage="output",
            data_type="number",
            binding_config={
                "expr": "unmet_heating_hours + unmet_cooling_hours",
            },
        ),
        # ... more signals, derivations ...
    ],
    # Template variable editing is handled by the unified signals card
    # (ADR-2026-03-10), not by step_editor_cards.
)
```

When a workflow step uses this validator with a parameterized IDF template, template variables
appear as input signals in the unified card, alongside any catalog INPUT entries. Authors can
edit each variable's annotations (label, default, type, constraints) via a per-variable modal.

## Validator lifecycle

1. **Discovery** — `register_validators()` runs inside `ValidationsConfig.ready()` at application
   startup. It calls `discover_configs()` to find package-based validators (those with a
   `config.py` module) and loads `BUILTIN_CONFIGS` for single-file validators. Both the config
   registry and the validator class registry are populated in a single pass.

2. **DB sync** — the `sync_validators` management command reads from the config registry and
   creates or updates `Validator` rows and their `ValidatorCatalogEntry` rows in the database.
   This runs at container startup and is idempotent. Custom validators are created separately
   through the Validator Library UI.

3. **Selection** — workflow steps reference a `Validator` via FK. The step editor UI uses
   the config registry to display available validators with their metadata, icons, and
   supported file types.

4. **Step editor resolution** — when the step detail page loads, it reads `step_editor_cards`
   from the validator's config, evaluates each card's `condition` callable, instantiates the
   `form_class` if provided, and renders the card template into the right column. This is how
   validators inject custom UI (like EnergyPlus's "Template Variables" card) without modifying
   the core step detail view.

5. **Execution** — the runtime calls `get_validator_class(validation_type)` to retrieve the validator
   class, instantiates it, and runs validation. The provider optionally instruments the uploaded
   artifact, binds helper closures, and the validator evaluates derivations followed by
   assertions. Findings are emitted with references back to the validator and catalog snapshot
   for auditability.

See [Assertions](assertions.md) for how the validator catalog is consumed by rulesets, and
how findings reference these slugs.
