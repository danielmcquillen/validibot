# SHACL validator

The SHACL validator validates RDF graphs (Turtle, JSON-LD, RDF/XML,
N-Triples, N-Quads) against SHACL shape collections. It is a built-in,
in-process validator — no Docker, no JVM, no network. It ships with the
community edition.

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
   - **SHACL shapes (required)** — upload one or more `.ttl` (or
     `.rdf`, `.jsonld`) files containing your shape declarations, or
     paste inline.
   - **Supplementary ontologies (optional)** — upload ontology files
     to give the reasoner context. Skip if your shapes file is also
     an ontology (true for ASHRAE 223P, where every class is
     simultaneously an ``sh:NodeShape``).
   - **Bundled standards** — checkboxes for Brick 1.4 and QUDT 2.1
     once Phase 2 ships their content. In Phase 1 these emit a
     warning when checked.
   - **Inference mode** — RDFS is the default and the right choice
     for 223P / Brick / Haystack work. Switch to OWL 2 RL only if your
     shapes genuinely need full OWL reasoning.
   - **Advanced SHACL** — leave on. Required for ASHRAE 223P (its
     shapes use ``sh:SPARQLConstraint`` for medium compatibility).
   - **Submission RDF format** — leave on **Auto-detect** unless the
     submitter's filename doesn't match the actual serialisation.
3. Save the step. The form runs an rdflib parse pass on every upload,
   so Turtle syntax errors appear inline before the workflow saves.

## Authoring CEL assertions

The SHACL engine emits these signals (always present, no extra
configuration needed):

| Signal | Type | Meaning |
|---|---|---|
| `o.parse_ok` | bool | RDF parse succeeded. |
| `o.parse_serialization` | string | The format used (`turtle`, `json-ld`, …). |
| `o.triple_count` | number | Total triples after parse. |
| `o.inferred_triple_count` | number | Triples added by the reasoner. |
| `o.namespaces_present` | list[string] | Namespace URIs seen in any triple. |
| `o.has_s223_namespace` | bool | Cheap check for ASHRAE 223P content. |
| `o.has_g36_namespace` | bool | Same for Guideline 36. |
| `o.has_brick_namespace` | bool | Same for Brick. |
| `o.shacl_violation_count` | number | Count of `sh:Violation` results. |
| `o.shacl_warning_count` | number | Count of `sh:Warning` results. |
| `o.shacl_info_count` | number | Count of `sh:Info` results. |
| `o.shacl_total_count` | number | All results across severities. |

Typical workflow gates:

```cel
o.parse_ok == true
o.has_s223_namespace == true
o.shacl_violation_count == 0
```

These assert the file parsed, the contractor actually used 223P
(not Brick), and the submission conforms to the SHACL shapes you
attached.

## What ends up in the run

Two artifacts:

- **Structured findings** — one per SHACL constraint violation, with
  severity mapped from `sh:resultSeverity` (Violation → ERROR,
  Warning → WARNING, Info → INFO). The finding's `meta` carries
  ``shacl_focus_node``, ``shacl_source_shape``, and
  ``shacl_constraint_component`` for display.
- **Native `sh:ValidationReport`** — the pyshacl-produced report graph,
  serialised as Turtle and attached to `result.stats["results_graph_turtle"]`.
  Downstream tools (BuildingMOTIF, analytics platforms, AI agents) can
  ingest this directly without going through Validibot's JSON.

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
fast enough in-process for the published 223P examples (largest is 2.6
MB / ~50K triples), and Docker boot overhead would dwarf the actual
validation work.

## Library-level custom SHACL validators

An organisation can create a named, org-owned SHACL validator (e.g.
``MeridianCx 223P + G36 Validator``) that bundles its shapes once and
gets reused across many workflows. The engine merges the library
validator's `default_ruleset` shapes with the step-level ruleset extras,
mirroring the assertion-merge pattern in
``BaseValidator.evaluate_assertions_for_stage``.

### Creation flow (operator-facing)

1. **Validator Library → New Validator** opens the validator-type
   picker modal. Pick **SHACL Validator (RDF graph rules)**.
2. Fill in the create form. The fields mirror the workflow step config
   form (shapes upload, ontology upload, bundled-standards checkboxes,
   engine knobs) plus validator-level metadata at the top (name,
   version, short description, description, notes).
3. Save. The service creates an org-owned `Validator` row
   (``validation_type=SHACL``, ``is_system=False``) with a populated
   ``default_ruleset`` carrying the shapes and metadata.
4. Workflow authors now see the new validator in the **Custom** tab of
   the validator library. Adding it to a workflow step inherits the
   bundled shapes via the engine's library + step ruleset merge.

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

## Testing patterns

Engine tests live in
[`validibot/validations/tests/test_validators/test_shacl_engine.py`](../../../validibot/validations/tests/test_validators/test_shacl_engine.py)
— pure-function tests, no Django setup, fast.

Integration tests live in
[`test_shacl_validator.py`](../../../validibot/validations/tests/test_validators/test_shacl_validator.py)
— exercise the full Django path including the library-validator
`default_ruleset` merge.

Form + builder tests live in
[`validibot/workflows/tests/test_shacl_form.py`](../../../validibot/workflows/tests/test_shacl_form.py).

Run all SHACL tests:

```bash
source set-env.sh
uv run --group dev pytest \
    validibot/validations/tests/test_validators/test_shacl_engine.py \
    validibot/validations/tests/test_validators/test_shacl_validator.py \
    validibot/workflows/tests/test_shacl_form.py
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

## Future phases

- **Phase 1b** — library-level custom SHACL validator creation UI
  (forms + view + URL + template).
- **Phase 2** — bundled Brick + QUDT static assets; 223P-specific
  signal profile (``o.equipment_count``, ``o.zones_with_co2_sensor_count``).
- **Phase 3** — curated fix-hint lookup table for the top 20 most
  common 223P shape violations.
- **Phase 4** — signed attestation payload extended with the SHACL
  shape file hashes + result counts (Pro only).
- **Phase 5+** — Haystack 4 / IFC-OWL preset shape collections; LLM-
  assisted shape authoring.
