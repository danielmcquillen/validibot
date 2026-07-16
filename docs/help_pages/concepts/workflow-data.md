<!--
In-app companion to the canonical public user overview at
validibot-marketing/docs/user_docs/concepts/data-namespaces.md. Keep the
architectural rules synchronized; links may differ between the two surfaces.
-->

# How Data Flows Through a Workflow

Validibot separates workflow data by where it came from, who named it, and
when it becomes available. The prefixes in an assertion are not interchangeable
aliases for one global data dictionary.

The governing rule is:

> A workflow contains many kinds of data, but only an author-named CEL/JSON
> value in `s.*` is a signal.

## Think of each step as a function

```text
step(inputs) -> outputs
```

- `i.*` contains the current step's value inputs—the function parameters.
- `o.*` contains its value outputs—the return value.
- `steps.<key>.*` gives later steps direct access to a completed step's values.
- `s.*` is shared workflow vocabulary selected by the workflow author.
- `p.*` is the raw submitted payload.
- `submission.*` is metadata about that submission.
- `c.*` contains fixed workflow constants.

## Namespace reference

| Short form | Long form | What it contains | When it is available |
|---|---|---|---|
| `p.*` | `payload.*` | Raw submitted content | Throughout the run when traversable |
| `submission.*` | — | Submission metadata and server facts | Throughout the run |
| `c.*` | `const.*` | Fixed workflow literals | Throughout the run |
| `s.*` | `signal.*` | Author-named values from mappings or promotions | Mapped values from run start; promoted values downstream |
| `i.*` | `input.*` | Current step's resolved value inputs | Input and output stages of that step |
| `o.*` | `output.*` | Current step's produced value outputs | Output stage of that step |
| `steps.<key>.input.*` / `steps.<key>.output.*` | — | Recorded values from an earlier step | Downstream after the relevant stage completes |

Tabular row rules also have a special `row.*` namespace containing the current
row. It exists only inside that row-evaluation lane.

## Signals are workflow vocabulary

A signal is an author-named CEL/JSON value. It is useful when a business concept
should have a stable name regardless of a payload's structure or the validator
that produced it.

You create signals in two ways:

1. **Signal mapping** gives a payload or submission value a workflow name before
   any steps run. For example:

   ```text
   p.project.targets.max_eui -> s.target_eui
   ```

2. **Copy to Signal** promotes a value-port step input or output after its stage
   completes. For example:

   ```text
   o.site_eui_kwh_m2 -> s.actual_eui
   ```

Mapped signals are available from the start of the run. Promoted signals are
available only after their source value exists, so they are downstream-only.

## Step inputs and outputs are ports

The Inputs and Outputs panels describe a step's contract: what it can consume,
what it can produce, the stable name and type of each item, and how inputs can
be connected.

The contract entry is not itself a signal. At runtime, a small input value may
appear as `i.<name>` and a small output value may appear as `o.<name>`. It enters
`s.*` only after explicit promotion.

An input binding supplies a value or artifact to one of those input ports. A
value input might be wired to `p.project.target`, `s.target`,
`c.default_target`, or an earlier step's output while keeping its local
`i.<name>` identity.

## Values and artifacts are separate

Value ports carry CEL/JSON-compatible values such as booleans, numbers,
strings, arrays, and objects. They can appear in expressions, travel through
`steps.*`, and be promoted into `s.*`.

Artifact ports carry files or file-like results such as FMUs, weather files,
reports, transformed documents, and logs. They require storage, authorization,
hashing, retention, and lineage. An artifact is never a signal and cannot be
copied into `s.*`.

A validator can expose small facts *about* an artifact—such as
`o.has_report`, `o.row_count`, or `o.file_format`—and those value outputs may be
used in expressions or promoted.

## Runtime lifecycle

1. The submission arrives, making `p.*`, `submission.*`, and `c.*` available.
2. Signal mappings resolve into the initial `s.*` vocabulary.
3. The current step's bindings and parser populate `i.*`.
4. Input-stage assertions run; the current step has no `o.*` yet.
5. The validator produces value outputs and any separate artifacts.
6. Output-stage assertions run with both `i.*` and `o.*` available.
7. Selected value inputs or outputs are promoted into `s.*`.
8. Later steps can also use direct `steps.<key>.*` references.

The assertion editor enforces stage availability. An input-stage assertion
cannot reference the current step's `o.*` values because they have not been
produced.

## Direct step references or signals?

Use a direct reference such as:

```cel
steps.energyplus.output.site_eui_kwh_m2
```

when the dependency intentionally belongs to that producer and contract key.
The reference preserves provenance.

Promote it to something like:

```cel
s.actual_eui
```

when it is shared workflow vocabulary, appears in several places, or should
remain stable if the producing validator changes. The signal expresses business
meaning.

## Choosing the right mechanism

- Raw submitted content: `p.*`
- Metadata about the submission: `submission.*`
- Fixed workflow policy or threshold: `c.*`
- Current step parameter or parsed input fact: `i.*`
- Current step result: `o.*`
- One-off dependency on an earlier producer: `steps.<key>.*`
- Shared author-named business vocabulary: `s.*`
- File or file-like input/output: an artifact port, not a CEL namespace

## Related guides

- [Signals, Step Inputs, and Step Outputs](/app/help/validators/signals/) — map
  and promote workflow values
- [CEL Expressions](/app/help/concepts/cel-expressions/) — write expressions
  using these namespaces
- [Data Paths](/app/help/validators/data-paths/) — navigate JSON and XML data
- [Validators Overview](/app/help/validators/validators-overview/) — choose a
  validator and understand its contract
