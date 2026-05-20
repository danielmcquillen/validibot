# SHACL validator

The SHACL validator validates RDF graphs (Turtle, JSON-LD, RDF/XML,
N-Triples, N-Quads) against SHACL shape collections. It is a built-in
validator that runs pySHACL in a killable Python subprocess — no Docker
or JVM required. It ships with the community edition.

Common configurations include ASHRAE 223P, Guideline 36, Brick Schema,
Project Haystack 4, and project-specific shapes the author uploads.

For the architectural rationale, design tradeoffs, and rejected
alternatives, see
[ADR-2026-05-18](../../../validibot-project/docs/adr/2026-05-18-shacl-validator-for-rdf-graph-validation.md)
in the project repo.

## When to use it

Pick the SHACL validator when the submission is an RDF graph and the
acceptance criteria are expressible as SHACL shapes. Typical use cases:

- A commissioning agent receives a 223P + G36 semantic model from a
  controls contractor and needs to verify it conforms to ASHRAE 223P
  shapes before passing it to the analytics platform.
- A smart-building software vendor ingests Brick models from customer
  buildings and wants a consistent pass/fail gate per ingest.
- A building owner has an internal data dictionary expressed as
  custom SHACL shapes and wants every contractor deliverable validated
  against it.

For non-RDF data (JSON, XML, EnergyPlus IDF), pick the matching native
validator.

## Setting up a step

1. Open a workflow, click **Add step**, pick **SHACL Validator** from
   the library.
2. The step config dialog presents:
   - **SHACL shapes (required)** — upload one or more Turtle `.ttl`
     files containing your shape declarations, or paste inline. Submitted
     RDF graphs may use Turtle, JSON-LD, RDF/XML, N-Triples, or N-Quads,
     but Phase 1 shape and ontology configuration files are Turtle-only.
   - **Supplementary ontologies (optional)** — upload Turtle `.ttl`
     ontology files to give the reasoner context. Skip if your shapes file is also
     an ontology (true for ASHRAE 223P, where every class is
     simultaneously an ``sh:NodeShape``).
   - **Bundled standards** — Brick 1.4 and QUDT 2.1 are not exposed in
     Phase 1. Phase 2 will add bundled-content controls once the
     license-clean assets ship.
   - **Inference mode** — RDFS is the default and the right choice
     for 223P / Brick / Haystack work. Switch to OWL 2 RL only if your
     shapes genuinely need full OWL reasoning.
   - **Advanced SHACL** — leave off unless the shapes require SHACL-AF
     features such as ``sh:SPARQLConstraint`` or SHACL Rules. Even when
     the step toggle is on, the deployment must also set
     ``SHACL_ENABLE_ADVANCED_FEATURES=True`` before embedded SHACL-AF
     constructs will run. SHACL-JS is never executed.
   - **Submission RDF format** — leave on **Auto-detect** unless the
     submitter's filename doesn't match the actual serialisation.
   - **SHACL result handling** — controls how normal SHACL validation
     results affect the step outcome:
     - **Fail immediately on violations** stops before author assertions
       if SHACL reports a violation.
     - **Fail after assertions** runs author assertions and then fails if
       SHACL reported a violation or an assertion failed. This is the
       recommended default because authors see all feedback from one run.
     - **Report only** exposes SHACL counts and the native report graph
       without turning SHACL validation results into blocking findings.
       Use this when explicit SPARQL ASK, Basic, or CEL assertions should
       decide pass/fail.
3. Save the step. The form runs an rdflib parse pass on every upload,
   so Turtle syntax errors appear inline before the workflow saves.

Result handling only applies to normal SHACL validation results. RDF
parse errors, invalid shapes, pySHACL timeouts, SPARQL assertion engine
errors, and other runtime failures always fail the step immediately.

Workflow allowed file types should match the RDF serializations authors
expect from submitters: **Plain Text** for Turtle, N-Triples, and
N-Quads; **JSON** for JSON-LD; and **XML** for RDF/XML.
These workflow-level choices are enforced before the SHACL validator
runs. A workflow that allows only **JSON** accepts JSON and JSON-LD
uploads, but rejects Turtle, N-Triples, N-Quads, and RDF/XML even though
the SHACL engine can parse them when the matching workflow file type is
enabled.

## Authoring assertions

Authors have two languages to choose from when writing a gate on a
SHACL step:

- **CEL** (or the Basic-gate UI) for scalar engine-level signals —
  parse status, triple counts, namespace lists, warning / info counts.
- **SPARQL ASK** for any question about the *contents* of the graph —
  per-shape conformance, project-specific rules, namespace allow-lists,
  referential integrity. Anything that requires looking at triples is
  a SPARQL ASK assertion.

The two have non-overlapping jobs. The platform always handles "did it
parse" and engine failures. SHACL violations are automatic gates in
**Fail immediately on violations** and **Fail after assertions** modes.
In **Report only** mode, SHACL violations become report data and the
author's assertions decide whether the step passes.

### Engine signals available in CEL

The SHACL engine emits a fixed set of output signals on every run,
identical regardless of which shapes or ontologies the author uploaded.
These appear in the Basic/CEL assertion picker as `o.*` targets.

| Signal | Type | Meaning |
|---|---|---|
| `o.parse_ok` | bool | Whether RDF parse succeeded. Parse failure already auto-fails the step, but the signal is available for reporting and CEL templates. |
| `o.parse_serialization` | string | The format used (`turtle`, `json-ld`, …). |
| `o.triple_count` | number | Total triples after parse. |
| `o.namespaces_present` | list[string] | Namespace URIs seen in any triple. |
| `o.has_s223_namespace` | bool | Whether the graph uses the ASHRAE 223P namespace. |
| `o.has_g36_namespace` | bool | Whether the graph uses the Guideline 36 namespace. |
| `o.has_brick_namespace` | bool | Whether the graph uses the Brick namespace. |
| `o.shacl_violation_count` | number | Count of `sh:Violation` results. Violations auto-fail unless the step uses **Report only** result handling. |
| `o.shacl_warning_count` | number | Count of `sh:Warning` results. |
| `o.shacl_info_count` | number | Count of `sh:Info` results. |
| `o.shacl_total_count` | number | Total number of SHACL results at all severities. |

Typical CEL / Basic gates:

```cel
o.triple_count >= 100              // sanity: file isn't effectively empty
o.shacl_warning_count == 0         // strict mode: no warnings allowed
"http://data.ashrae.org/standard223#" in o.namespaces_present
```

### SPARQL ASK assertions

For any question that depends on graph contents — per-shape conformance,
class composition, referential integrity, project rules — write a SPARQL
ASK assertion from the step editor:

1. Open the SHACL step.
2. Click **Add assertion**.
3. Choose **SHACL** as the assertion type. This option appears only for
   steps whose validator is `ValidationType.SHACL`.
4. Fill in the description, target graph, SPARQL ASK query, severity,
   and failure/success messages.

Each saved ASK is a normal `RulesetAssertion` row with
`assertion_type=SHACL`, `operator=SPARQL_ASK`, and its query stored in
`rhs`. That keeps ordering, severity, messages, and `assertion_id`
attribution consistent with the rest of the assertion system.

Target graph options:

- **Submitted RDF data graph** (`data`) — the parsed submission graph
  and the most common target.
- **SHACL results graph** (`results`) — the `sh:ValidationReport` graph
  produced by pySHACL. Use this for report-level gates such as
  `ASK { FILTER NOT EXISTS { ?r a sh:ValidationResult ; sh:sourceShape ex:DamperShape . } }`.
- **Data + results graph** (`union`) — both graphs combined. Use this
  to join findings back to submitted resources.

The results graph is RDF generated by the SHACL engine after validation.
It usually contains one `sh:ValidationReport` node and zero or more
`sh:ValidationResult` nodes. Each result can record the focus node, value,
source shape, result path, severity, and message. Query this graph when
the assertion is about the validation report itself, for example "no
violations came from `ex:DamperShape`".

Only SPARQL 1.1 `ASK` is supported in this release. `SELECT`,
`CONSTRUCT`, `DESCRIBE`, Update operations, `SERVICE`, and remote
`FROM`/`FROM NAMED` references are rejected at assertion save time and
re-scrubbed again at validation time. The assertion textarea exposes
the same configured length cap via `maxlength`; server-side validation
also rejects anything over `SHACL_SPARQL_QUERY_LENGTH_MAX` (default
10,000 characters, hard-clamped to 50,000).

Example: enforce that every `s223:Zone` has a CO2 sensor by checking
the data graph for the inverse:

```sparql
PREFIX s223: <http://data.ashrae.org/standard223#>
PREFIX qudt: <http://qudt.org/schema/qudt/>
PREFIX quantitykind: <http://qudt.org/vocab/quantitykind/>

ASK {
  FILTER NOT EXISTS {
    ?zone a s223:Zone .
    FILTER NOT EXISTS {
      ?sensor s223:hasObservationLocation/^s223:hasDomainSpace ?zone ;
              s223:observes ?p .
      ?p qudt:hasQuantityKind quantitykind:MoleFraction .
    }
  }
}
```

#### What the SPARQL scrubber refuses

At assertion save time the scrubber walks the query's algebra tree and
refuses any of:

- Top-level form other than `ASK` (no SELECT, CONSTRUCT, DESCRIBE,
  Update).
- `SERVICE` federation clauses (the canonical exfiltration vector).
- `LOAD` / `INSERT` / `DELETE` / `CLEAR` / `DROP` / `CREATE` / `ADD` /
  `MOVE` / `COPY` operations.
- `FROM` / `FROM NAMED` referencing non-default graphs.
- Property paths nested past the configured depth cap (default 8).
- Total query length above the cap (default 10,000 characters).

Each rejection produces a clear inline error naming the construct that
triggered it, so you can fix the query without consulting the source.

## What ends up in the run

Three artifacts:

- **Structured findings** — in the two fail-on-violation modes, one per
  SHACL constraint violation, with severity mapped from
  `sh:resultSeverity` (Violation → ERROR, Warning → WARNING, Info →
  INFO). The finding's `meta` carries ``shacl_focus_node``,
  ``shacl_source_shape``, and ``shacl_constraint_component`` for display.
  In **Report only** mode, native SHACL validation results are not
  converted into Validibot findings; assertion findings still appear.
- **Native `sh:ValidationReport`** — the pyshacl-produced report graph,
  serialised as Turtle and attached to `result.stats["results_graph_turtle"]`.
  Downstream tools (BuildingMOTIF, analytics platforms, AI agents) can
  ingest this directly without going through Validibot's JSON.
- **Output signals** — scalar `o.*` values such as
  `o.shacl_violation_count` and `o.shacl_total_count`, available to
  Basic and CEL assertions regardless of result-handling mode.

## Engine architecture

The validator lives at
[validibot/validations/validators/shacl/](../../../validibot/validations/validators/shacl/)
and splits into:

- `config.py` — `ValidatorConfig` registration with the validator
  catalogue.
- `validator.py` — the orchestrator class, `SHACLValidator`. Thin —
  it just sequences the engine functions.
- `engine.py` — the pure functions: parse, infer, run pyshacl, map
  results, extract signals. No Django imports. Unit-tested without
  the test database.

Dependencies (all pure Python):

- [pyshacl](https://github.com/RDFLib/pySHACL) (Apache 2.0) — SHACL
  engine.
- [rdflib](https://github.com/RDFLib/rdflib) (BSD-3-Clause) — RDF
  parsing.
- [owlrl](https://github.com/RDFLib/OWL-RL) (W3C SD License) — OWL 2
  RL reasoner.

The validator does NOT run in an advanced (Docker) validator container.
See the ADR for the cost-benefit analysis — short version: pyshacl is
fast enough for the published 223P examples (largest is 2.6 MB / ~50K
triples), and Docker boot overhead would dwarf the actual validation
work. The pySHACL call still runs out-of-process so the engine can
terminate pathological shape/data combinations on timeout. This uses a
plain subprocess rather than ``multiprocessing.Process`` so it works
inside Celery prefork workers, whose task processes are daemonic.

## Library-level custom SHACL validators

An organisation can create a named, org-owned SHACL validator (e.g.
``MeridianCx 223P + G36 Validator``) that bundles its shapes once and
gets reused across many workflows.

For new workflow steps, the step builder snapshots the library
validator's `default_ruleset` into the step-owned SHACL ruleset at save
time. That snapshot includes the library shapes, ontology text, and
hashes/provenance metadata. SHACL SPARQL ASK assertions are authored on
individual workflow steps through the **Add assertion** dialog. This
means a later edit to the library validator does not silently change an
existing workflow step. Older steps that predate the snapshot flag still
use the legacy live-merge path: the engine merges `validator.default_ruleset`
with the step-level ruleset extras.

### Creation flow (operator-facing)

1. **Validator Library → New Validator** opens the validator-type
   picker modal. Pick **SHACL Validator (RDF graph rules)**.
2. Fill in the create form. The fields mirror the workflow step config
   form (shapes upload, ontology upload, engine knobs) plus
   validator-level metadata at the top (name, version, short
   description, description, notes).
3. Save. The service creates an org-owned `Validator` row
   (``validation_type=SHACL``, ``is_system=False``) with a populated
   ``default_ruleset`` carrying the shapes and metadata.
4. Workflow authors now see the new validator in the **Custom** tab of
   the validator library. Adding it to a workflow step snapshots the
   bundled shapes into the step ruleset so the workflow has stable
   validation behavior.

### Edit + delete

- Open the validator's detail page in the library and use the standard
  **Edit** / **Delete** actions. The edit form has *keep-existing*
  semantics: leave the shapes upload + paste areas blank to preserve
  the existing content; only metadata + engine knobs refresh.
- Delete is blocked when any workflow step still references the
  validator. The error message lists the blocking workflows so the
  author can detach them first.

### Architecture pieces

| Concern | Location |
|---|---|
| Form | ``ShaclLibraryValidatorCreateForm`` / ``ShaclLibraryValidatorUpdateForm`` in [validibot/validations/forms.py](../../../validibot/validations/forms.py) |
| Service | ``create_shacl_library_validator`` / ``update_shacl_library_validator`` in [validibot/validations/utils/__init__.py](../../../validibot/validations/utils/__init__.py) |
| Views | ``ShaclLibraryValidatorCreateView`` / ``...UpdateView`` / ``...DeleteView`` in [validibot/validations/views/validators.py](../../../validibot/validations/views/validators.py) |
| URL routes | ``library/shacl/new/``, ``library/shacl/<slug>/edit/``, ``library/shacl/<slug>/delete/`` |
| Library modal entry | ``_build_validator_create_options`` in [validibot/validations/views/library.py](../../../validibot/validations/views/library.py) |
| Template | ``validations/library/custom_validator_form.html`` — shared with Custom validator forms; renders any crispy-forms layout |
| Shared form mixin | ``ShaclConfigMixin`` in [validibot/validations/validators/shacl/form_fields.py](../../../validibot/validations/validators/shacl/form_fields.py) — used by both the workflow step config form and the library validator forms |
| Persistence helpers | ``concatenate_uploaded_files`` / ``read_uploaded_text`` in [validibot/validations/validators/shacl/persistence.py](../../../validibot/validations/validators/shacl/persistence.py) |
| SPARQL assertion form | ``RulesetAssertionForm`` in [validibot/validations/forms.py](../../../validibot/validations/forms.py) — exposes the SHACL assertion type only for SHACL validator steps |

## Testing patterns

Engine tests live in
[`validibot/validations/tests/test_validators/test_shacl_engine.py`](../../../validibot/validations/tests/test_validators/test_shacl_engine.py)
— pure-function tests, no Django setup, fast.

Integration tests live in
[`test_shacl_validator.py`](../../../validibot/validations/tests/test_validators/test_shacl_validator.py)
— exercise the full Django path including legacy library-validator
`default_ruleset` merge and the newer step-level snapshot path.

Form + builder tests live in
[`validibot/workflows/tests/test_shacl_form.py`](../../../validibot/workflows/tests/test_shacl_form.py).
The Add Assertion dialog and SHACL `RulesetAssertion` persistence are
covered in
[`validibot/workflows/tests/test_workflow_assertions.py`](../../../validibot/workflows/tests/test_workflow_assertions.py).

Run all SHACL tests:

```bash
source set-env.sh
uv run --group dev pytest \
    validibot/validations/tests/test_validators/test_shacl_engine.py \
    validibot/validations/tests/test_validators/test_shacl_security.py \
    validibot/validations/tests/test_validators/test_shacl_validator.py \
    validibot/validations/tests/test_validators/test_shacl_library_validator.py \
    validibot/workflows/tests/test_shacl_form.py \
    validibot/workflows/tests/test_workflow_assertions.py
```

## Common issues

**"Failed to parse submission as json-ld"** when the file is actually
Turtle.

Cause: the form's auto-detect picked JSON-LD because the
``SubmissionFileType`` on the upload was ``JSON``. Fix: set the step
config's **Submission RDF format** to **Turtle (.ttl)** explicitly, or
ensure the upload comes in with the right MIME type.

**"No SHACL shapes were supplied"** even though the ruleset has a
default_ruleset attached.

Cause: the library validator's ``default_ruleset.rules_text`` is empty.
Fix: open the validator in the library, upload shapes, save.

**Bundle warnings on every run** ("Bundled standard 'brick-1.4' is
recognised but the shapes file ships in Phase 2 …").

Expected behaviour for Phase 1. Either upload the Brick shapes
yourself or wait for Phase 2 to ship the bundled content.

## Security

The SHACL validator's threat surface is unusually large because it
executes two distinct classes of attacker-controllable input — shapes
and ontologies from the author, plus the RDF submission. Several
hardenings run on every validation; see ADR-2026-05-18 "Security" for
the full threat model and acceptance-test list.

The headline mitigations:

- **JSON-LD context documents rejected pre-parse.** A submission whose
  context references `http://attacker.com/log`, a relative
  `./context.jsonld`, or a nested/property-scoped context document is
  refused before rdflib's parser sees it. Inline context objects and
  `data:` contexts are allowed. The scanner is in
  `engine.prevalidate_safety`.
- **RDF/XML XXE constructs rejected pre-parse.** `<!DOCTYPE` and
  `<!ENTITY` declarations are refused — the canonical local-file-
  exfiltration vector. Same scanner.
- **Advanced SHACL is deployment-gated.** Core SHACL runs by default.
  Embedded SHACL-AF/SPARQL constraints and SHACL Rules require both the
  step/library toggle and `SHACL_ENABLE_ADVANCED_FEATURES=True`.
  SHACL-JS constructs are rejected before pySHACL starts.
- **pyshacl subprocess timeout.** The engine launches pySHACL in a
  plain Python subprocess and terminates it if it exceeds
  `SHACL_VALIDATION_TIMEOUT_SECONDS` (default 30 s, hard-capped at
  120 s). This avoids Celery prefork's restriction against
  multiprocessing children from daemonic task processes.
- **pyshacl JS off, owl:imports off.** Hard-coded as kwargs on every
  `pyshacl.validate` call; the validate kwargs cannot be overridden.
- **SPARQL ASK queries scrubbed at form save.** The scrubber
  ([`sparql_security.py`](../../../validibot/validations/validators/shacl/sparql_security.py))
  rejects SELECT / CONSTRUCT / DESCRIBE / Update operations,
  `SERVICE` federation, `LOAD`, `FROM` / `FROM NAMED` with non-default
  IRIs, deeply nested property paths, and pathologically long queries.
- **Per-query wall-clock timeout.** Defaults to 10 s (capped at 60 s).
  Each custom ASK runs in a short-lived Python subprocess, so timeout
  can terminate the query instead of leaving `rdflib.Graph.query()`
  running inside the Django or Celery worker.
- **All engine errors become findings.** No exception escapes the
  validator. Timeouts, scrub rejections, runtime crashes, and corrupted
  SPARQL assertion metadata all surface as `shacl.*` findings.

Resource limits (overridable via Django settings, capped by hard
maximums the operator cannot exceed):

| Setting | Default | Hard cap |
|---|---|---|
| `SHACL_ENABLE_ADVANCED_FEATURES` | `False` | n/a |
| `SHACL_MAX_DATA_TRIPLES` | 100,000 | 1,000,000 |
| `SHACL_MAX_SHAPE_TRIPLES` | 50,000 | 200,000 |
| `SHACL_MAX_ONTOLOGY_TRIPLES` | 100,000 | 500,000 |
| `SHACL_MAX_VALIDATION_DEPTH` | 25 | 50 |
| `SHACL_VALIDATION_TIMEOUT_SECONDS` | 30 | 120 |
| `SHACL_SPARQL_QUERY_TIMEOUT_SECONDS` | 10 | 60 |
| `SHACL_SPARQL_QUERY_LENGTH_MAX` | 10,000 chars | 50,000 chars |
| `SHACL_SPARQL_PROPERTY_PATH_DEPTH_MAX` | 8 | 32 |
| `SHACL_SPARQL_ASKS_PER_STEP_MAX` | 25 | 100 |

## Future phases

- **Phase 2** — bundled Brick + QUDT static assets (license-clean
  redistributable content); re-enable the bundled-standards checkboxes
  hidden in Phase 1.
- **Phase 3** — curated fix-hint lookup table for the top 20 most
  common 223P shape violations.
- **Phase 4** — signed attestation payload extended with the SHACL
  shape file hashes + result counts (Pro only).
- **Phase 5+** — Haystack 4 / IFC-OWL preset shape collections;
  named SPARQL SELECT signals for arithmetic composition (deferred
  to a separate ADR triggered by real customer demand); LLM-assisted
  shape authoring.
