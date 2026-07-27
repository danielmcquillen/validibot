# Building Benchmark Report Validator

The Building Benchmark Report Validator checks property report exports from
ENERGY STAR® Portfolio Manager® and turns their metrics into stable workflow
outputs. It is
intended for programs that receive reports from many different building owners,
so it does not ask the workflow author to enter one expected property ID.

The validator accepts a single `.xls`, `.xlsx`, or XML property report, or a ZIP
containing independent property reports. Every report must represent one
property or one grouped parent and one reporting cycle. Spreadsheet, XML, and
archive parsing runs in the isolated Portfolio Manager validator backend.

## Choose a submission structure

**Single property report** is appropriate when every submission contains one
Portfolio Manager report. The optional **EUIt** field is the target for that
report. Measured Site EUI and Weather Normalized Site EUI are still output when
no target is configured.

**ZIP collection** is appropriate when one submitter sends many reports for one
reporting cycle. Each archive member can be XLS, XLSX, or XML. ZIP mode adds
limits for the number of reports, each report's size, and total expanded size.
It also exposes reporting-cycle consistency, duplicates, roster coverage,
parent/child overlap, target coverage, and portfolio aggregates.

Hidden operating-system metadata is ignored. Nested directories, nested
archives, encrypted members, duplicate paths, path traversal, unsupported
members, and unsafe compression ratios are rejected.

The step editor shows one matching assertion-output group:

- **Single property outputs** for single property reports; or
- **Grouped property outputs** for ZIP collections.

Changing the submission structure changes the available output list. If an
existing assertion uses an output from the current group, Validibot blocks the
change and lists the affected `o.*` values. Update or remove those assertions
first; Validibot never silently deletes or rewrites them.

## Configure the workflow's validation policy

The validator does not include regulatory profiles or infer requirements from
the workflow name. Authors configure every reporting-period, metric, target,
and data-quality requirement explicitly. The saved fields are the complete
policy executed by the backend.

An organization can name a workflow to communicate its intended use, such as
**Washington CBPS Workflow — 2026 reporting cycle**, but that name is only a
human-readable label. Authors remain responsible for checking that the selected
settings reflect their program's current requirements.

The available fields can express a Washington-oriented readiness workflow,
including a complete reporting period, an optional freshness limit, the State
of Washington Clean Buildings Standard ID, Weather Normalized Site EUI, the
Washington Form C metric bundle, and explicit Portfolio Manager Alert Metric
policies. These are independent controls rather than a Washington preset.

The validator does not calculate EUIt, validate Form B's target calculation,
confirm qualified-person work, determine exceptions, issue a legal compliance
decision, or produce an `is_compliant` result.

## Data-quality policies

The form provides Allow, Warning, or Error policies for these Portfolio Manager
facts:

- energy meter has less than 12 full calendar months of data;
- energy meter has gaps;
- energy meter has overlaps;
- no energy meters are selected for metrics;
- an energy meter has a single entry longer than 65 days;
- estimated energy values; and
- other Alert Metrics included in the report.

An enabled check requires its corresponding Alert Metric column. A missing
column is reported as **not verifiable from this export**; absence is never
treated as proof that an alert is clear.

## EUIt and target comparison

EUIt is author-supplied. The validator never derives it from the report.

In single mode, enter an optional EUIt directly in the step. In ZIP mode, that
same value is the default target. A matched Expected Buildings List value
overrides it. If your policy target comes from a workflow constant, leave the
native comparison off and use `c.*` with the extracted `o.*` values in CEL.

For a property with both WNEUI and a resolved target, the validator outputs:

- `weather_normalized_site_eui_kbtu_ft2_yr`;
- `resolved_euit_kbtu_ft2_yr` and its source;
- `euit_margin_kbtu_ft2_yr`, calculated as EUIt minus WNEUI;
- `euit_ratio`;
- `euit_percent_difference`, positive when the property is below target;
- `meets_euit`; and
- `near_euit`, an advisory band for a property that is above target.

The near-target band never changes `meets_euit`. Leave the native comparison
off when your program needs custom tolerance and write an output-stage CEL
assertion instead, for example:

```cel
o.weather_normalized_site_eui_kbtu_ft2_yr <= 1.1 * c.euit
```

## Expected Buildings List

ZIP mode can attach an Expected Buildings List (EBL). The EBL provides a roster
for reconciliation and optional per-building EUIt overrides. It does not make a
missing or unexpected building fail automatically; those remain outputs for
CEL or later workflow steps.

The V1 EBL is UTF-8 JSON:

```json
{
  "schema_version": "1.0",
  "id_field": {
    "kind": "standard_id",
    "name": "State of Washington Clean Buildings Standard"
  },
  "euit_unit": "kBtu/ft2/year",
  "buildings": [
    {"id_value": "1234567", "euit": "40.0"},
    {"id_value": "7654321"}
  ]
}
```

Supported identity kinds are `property_id`, `parent_property_id`,
`standard_id`, and `custom_id`. Named Standard and Custom IDs require the exact
Portfolio Manager label. IDs stay strings so leading zeroes are preserved.
EUIt values are positive decimal strings.

Validibot validates duplicate JSON keys, schema version, units, duplicate or
blank IDs, decimal targets, document size, entry count, nesting, keys, and text
length. The accepted file is stored as a content-hashed workflow-step resource
and is bundled with VAF workflow exports.

Reconciliation outputs include expected, matched, missing, unexpected, and
duplicate counts. Typical program policy remains an explicit CEL assertion:

```cel
o.missing_expected_building_count == 0
```

## Grouped property outputs

ZIP mode exposes bounded scalar outputs for CEL, including file/property
counts, reporting-cycle consistency, roster reconciliation, properties with
and without targets, properties meeting and above EUIt, target coverage and
compliance percentages, floor area meeting target, total floor area,
GFA-weighted WNEUI, score coverage and weighted score, and estimated excess
energy.

The `property_results` JSON artifact retains collection summary metrics,
per-report IDs, dates, metrics, metric evidence states, Alert Metric states,
target provenance, comparison values, reconciliation details, and attributed
findings. Unbounded property arrays do not enter the CEL environment.

If a ZIP includes both a parent and its child, or duplicate property IDs would
make population totals ambiguous, affected portfolio aggregates are
unavailable rather than double-counted. The overlap or duplicate remains an
observational fact unless the workflow author makes it an error.

## Trademark and independence notice

ENERGY STAR and the ENERGY STAR mark are registered trademarks owned by the
U.S. Environmental Protection Agency. The name ENERGY STAR Portfolio Manager
and the Portfolio Manager logo are registered trademarks owned by the U.S.
Environmental Protection Agency.

Validibot is independent. This validator is not approved, certified, or
endorsed by the EPA or the ENERGY STAR program. It checks the contents of
exported reports; it does not confer ENERGY STAR certification or determine
regulatory compliance.
