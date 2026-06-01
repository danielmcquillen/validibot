# Darwin Core occurrence — Tabular Validator example assets

These assets back the Darwin Core walk-through for the **Tabular Validator**
(`validibot.validations.validators.tabular`). They double as the fixtures for
`tests/tests_use_cases/test_darwin_core_tabular.py` and as the copy-paste
material for the accompanying blog post.

## Background

[Darwin Core](https://dwc.tdwg.org/) (DwC) is the TDWG standard for sharing
biodiversity occurrence records. It defines a flat vocabulary of named terms —
`occurrenceID`, `scientificName`, `decimalLatitude`, `eventDate`,
`basisOfRecord`, and so on — and controlled value sets for a few of them.
[OBIS](https://obis.org/) (the Ocean Biodiversity Information System) ingests
data *as* Darwin Core and runs a quality-control pass over it
([iobis/obis-qc](https://github.com/iobis/obis-qc)). This example reproduces the
first tier of those checks — the ones that can be expressed as a column schema
plus per-row rules — using nothing but a Validibot ruleset.

## Files

| File | Purpose |
|---|---|
| `occurrence_schema.json` | The Frictionless **Table Schema** descriptor. Goes on the ruleset's `rules` (the structured column config). Declares the DwC columns, types, and constraints. |
| `occurrence_valid.csv` | Four clean marine occurrence records. Passes the schema with zero findings. |
| `occurrence_invalid.csv` | One row per native check — each row trips exactly one column-level rule (plus a duplicate `occurrenceID`). |
| `occurrence_cross_field.csv` | Rows that are valid column-by-column but break a **cross-field** rule (depth ordering, Null Island). These are caught by CEL row assertions, not the schema. |
| `occurrence_schema_valid_assertion_invalid.csv` | The headline fixture: every row **passes the Frictionless schema** with zero findings, yet two of three rows are caught only by **manual assertions** (see below). |

## What the schema enforces (native, per-column)

- `occurrenceID` — the `primaryKey`: must be present, non-null, and unique.
- `basisOfRecord` — required; must be one of the eight DwC basis values.
- `occurrenceStatus` — must be `present` or `absent`.
- `scientificName` — required (non-empty).
- `scientificNameID` — must match a WoRMS LSID (`urn:lsid:marinespecies.org:taxname:<digits>`).
- `eventDate` — required; ISO-8601-ish. See the note below.
- `decimalLatitude` / `decimalLongitude` — required numbers in `[-90, 90]` / `[-180, 180]`.
- `coordinateUncertaintyInMeters`, `minimumDepthInMeters`, `maximumDepthInMeters` — non-negative numbers.
- `individualCount` — non-negative integer (how many individuals were recorded).

## What the schema can NOT enforce (needs CEL row assertions)

Cross-field, conditional, and strict-bound rules are not expressible in a flat
Table Schema, so they live as `row.*` CEL assertions on the ruleset
(`options.tabular_stage = "row"`):

- **Depth ordering** — `row.minimumDepthInMeters <= row.maximumDepthInMeters`
- **Null Island guard** — `!(row.decimalLatitude == 0.0 && row.decimalLongitude == 0.0)`
- **Presence implies a count** — `row.occurrenceStatus != "present" || row.individualCount >= 1`
  (a cross-field conditional: an `individualCount` of 0 means an *absence*, never a `present` record — mirrors obis-qc)
- **Uncertainty must be positive** — `row.coordinateUncertaintyInMeters > 0.0`
  (the schema's `minimum: 0` is *inclusive*, so 0 passes it; Darwin Core says zero is not a valid uncertainty)

## Schema-valid vs. meaningful — why both lanes exist

`occurrence_schema_valid_assertion_invalid.csv` is the clearest illustration of
the split. Run it against the schema **alone** and it passes with zero findings —
every cell is the right type, in range, and in the right vocabulary. Add the two
assertions above and two of the three rows fail:

- a `present` row with `individualCount = 0` (schema-valid integer, but
  semantically an absence), and
- a row with `coordinateUncertaintyInMeters = 0` (schema-valid by the inclusive
  `minimum: 0`, but a meaningless uncertainty).

No native finding fires for either — the failures are *purely semantic*. That is
the whole reason manual assertions sit alongside the Frictionless schema: the
schema checks **shape**, the assertions check **meaning**.

## A note on `eventDate`

`eventDate` is typed as `string` + regex, **not** `date`. The validator's `date`
type coerces full ISO 8601 only, but Darwin Core legitimately allows truncated
dates (`2009`, `2009-02`) and intervals (`2009-02-20/2009-03-01`). Typing it as
`date` would reject those valid DwC values as type errors, so the regex permits
year / year-month / date / date-interval while still rejecting free text.

## Out of scope

The deeper OBIS checks — "is this point on land?", "is this deeper than the
seafloor?", "does this name resolve in WoRMS?" — require external reference data
(coastlines, bathymetry, the WoRMS register) and cannot be done by a pure
tabular schema. Those would be a separate validator backend or an AI validation
step, not part of this example.
