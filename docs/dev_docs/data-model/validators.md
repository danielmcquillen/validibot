# Validators

Validators define the concrete execution engine that a workflow step will call. They bundle the technical
contract (validation type + provider version), the catalog of signals/derivations the engine exposes,
and optional organization-specific extensions (custom validators). The platform now ships with five stock
validation types:

- **BASIC** — no provider backing; authors add assertions manually via the UI. These steps still produce
  rulesets so findings remain auditable, but there is no catalog beyond whatever the assertion references.
- **JSON_SCHEMA / XML_SCHEMA** — schema validations that require uploading or pasting schema content.
- **ENERGYPLUS** — advanced simulation validators with IDF/simulation options and catalog entries.
- **AI_ASSIST** — template-driven AI validations (policy check, critic, etc.).
- **CUSTOM_VALIDATOR** — organization-defined validators registered via the custom validator UI (displayed as “Custom Basic Validator”).

Validators are stored in the `validators` table. Each row records the following:

- `slug`, `name`, `description`, `validation_type`, `version`
- relationship to an organization (`org_id`) and `is_system` flag
- timestamp fields plus the related `custom_validator` entry when the row was created by an org

Every workflow step references a validator row. During execution the validator tells the runtime which
provider class to load, which helper functions are legal, and how to interpret rule and assertion catalogs.

## Catalog entries (signals, outputs, derivations)

Validators own the canonical catalog describing what the validation engine can read or emit. Catalog
rows live in `validator_catalog_entries` and carry:

| Field | Meaning |
| --- | --- |
| `entry_type` | `signal` or `derivation`. |
| `run_stage` | Whether the entry is available during the **input** phase (before the engine runs) or the **output** phase (after the engine completes). |
| `slug` | Stable identifier referenced by rulesets and assertions. |
| `data_type` | Scalar/list metadata (number, datetime, bool, series). |
| `binding_config` | Provider-specific hints (e.g., EnergyPlus meter path). |
| `metadata` | Free-form JSON used by the UI and provider tooling. |

Inputs represent values already available before the engine runs (project metadata, uploaded files,
environment). Outputs represent telemetry the engine emits during execution. Derivations describe
computed metrics, and their `run_stage` flag indicates whether they enrich inputs or post-process
validator outputs. By centralising these definitions on the validator we let every ruleset reuse them without duplicating structure inside each rule. Workflow step authors can still define as many assertions as necessary by referencing the catalog slugs stored on the validator; see [Ruleset Assertions](assertions.md) for how those references are persisted and executed.
Basic validators intentionally skip catalog management; every assertion directly references the custom
target path the author entered.

## Validator rules (vs. workflow assertions)

- **Rules**: logic defined on a validator itself (for example, default CEL expressions). Stored on `validator_catalog_rules` and can reference one or more catalog entries via `validator_catalog_rule_entries`. Rules are evaluated according to the validator’s engine and ordering; deleting a rule cleans up its links.
- **Assertions**: logic defined on workflow steps against a ruleset. Stored separately and evaluated in workflow runs. Assertions are the only logic workflow authors manage today.

Catalog entries cannot be deleted while referenced by rules; rules can be deleted at any time (links are removed).

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

## Validator lifecycle

1. **Registration** — migrations or bootstrap logic call `create_default_validators()` to ensure every
   stock validator row and catalog entry exists. Custom validators are created through the Validator
   Library UI and stored per org.
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
