# Workflow import/export fixtures

Committed example artifacts for the workflow import/export feature
(`validibot/workflows/services/io/`). They are both the worked example for the
docs and the fixtures the import tests load.

## Files

| File | What it is |
|---|---|
| `darwin_core.json` | The Darwin Core workflow **definition** (`workflow.json` shape): one Tabular Validator step whose ruleset is the Darwin Core occurrence Table Schema, plus the four row-rule assertions (depth ordering, Null Island, presence-implies-count, positive uncertainty). The file-free import path. |
| `darwin_core.vaf` | The same definition packaged as a `.vaf` archive (a ZIP holding `manifest.json` + `workflow.json`). The archive import path. |

Both import to the identical workflow; having both lets the tests cover the
`.json` and `.vaf` paths.

## Regenerating

These are generated — don't hand-edit them. After changing the Table Schema asset
(`tests/assets/csv/darwin_core/occurrence_schema.json`) or the example shape,
regenerate:

```bash
python manage.py build_darwin_core_example
```

The command builds the definition from real enum values and the real Table Schema
asset, so the fixtures can't drift from what the importer expects. The packer is
deterministic, so regeneration is byte-stable (no spurious diffs).

## Who uses them

- `validibot/workflows/tests/test_workflow_io.py` — imports both files and
  asserts the rebuilt workflow (1 step, Table Schema ruleset, 4 row assertions),
  plus validator-resolution and the tabular column guard.
- `validibot/workflows/tests/test_workflow_io_views.py` — uploads `darwin_core.vaf`
  through the import view and checks the results page.

## See also

- Developer reference: `docs/dev_docs/data-model/workflow-import-export.md`
- The Darwin Core data + Table Schema: `tests/assets/csv/darwin_core/README.md`
