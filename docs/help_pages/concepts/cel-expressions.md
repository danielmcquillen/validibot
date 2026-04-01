# CEL Expressions

Validibot uses Common Expression Language (CEL) for advanced assertions. CEL is a simple, safe expression syntax for writing small, fast, readable conditions and rules over your data.

You can use CEL to perform simple assertion logic on your incoming data,
or data produced by your validator (e.g. after FMU Validator runs a submission through a simulation and produces output).
Whenever you add an assertion to your workflow step, you can base it on a CEL expression.

When the user submits data, each assertion runs. If the assertion has a CEL, and the CEL evaluates to false, the error message you provided in the
assertion will be added to the messages returned to the user.

## Data Namespaces

Every CEL expression runs in a context where your data is organized into clear namespaces. Each namespace gives you access to a different category of data, and you reference values using a short prefix.

| Short form | Long form | What it accesses |
|------------|-----------|------------------|
| `p.key` | `payload.key` | Raw submission data -- the JSON or XML the submitter sent |
| `s.name` | `signal.name` | Author-defined signals -- values you've named via signal mapping or promoted outputs |
| `output.name` | `o.name` | This step's validator outputs -- values produced by the current validator |
| `steps.step_key.output.name` | | Upstream step outputs -- outputs from a previously completed workflow step |

The `p.` namespace gives you direct access to whatever the submitter sent. If the submission payload contains `{"price": 20.00}`, you reference it as `p.price`.

The `s.` namespace gives you access to signals -- named values that you define at the workflow level. Signals abstract away the raw data structure, so your assertions stay readable even when the underlying data paths are complex. You create signals through signal mapping (on the workflow page) or by promoting a validator's outputs.

The `output.` namespace gives you the results produced by the current step's validator. For example, after an EnergyPlus simulation runs, `output.site_eui_kwh_m2` contains the simulated energy use intensity.

The `steps.` namespace lets you reference outputs from earlier workflow steps. If step "schema_check" produced an output called "record_count", you'd access it as `steps.schema_check.output.record_count`.

**What data can you reference?**

The names available in a CEL expression depend on what's been defined for the current workflow step:

- **Signals** (`s.name`): named values defined by the workflow author through signal mapping or output promotion.
- **Payload paths** (`p.key`): direct paths into the raw submission data, if the current validator supports direct data paths.
- **Outputs** (`output.name`): values produced by the current step's validator.
- **Upstream outputs** (`steps.key.output.name`): outputs from earlier workflow steps.

**Why not just use JSON or XML Schemas?**

Yes, it's true, you can use the Validibot JSON Schema validator or XML Schema validator for your workflow steps. In this case you don't
define individual rules, you just create one big schema and attach it to your validator. Schemas are great for structure: making sure fields exist, have the right type, follow enums, match patterns, etc. They’re the first line of defense for data integrity.

CEL expressions, on the other hand, handle the behavioural and cross-field rules for data that schemas either can’t express cleanly or make horribly verbose — things like numeric relationships between fields, tolerances, conditional requirements, or checks on simulation outputs from an FMU Validator. In Validibot, schemas define "what the data looks like"; CEL assertions define "what must be true about this data for it to be acceptable." They’re complementary, not competing.

You could create a workflow that has both a JSON schema validation and then some assertions that use detailed CEL expressions.

**Examples**

Here are some examples of CEL expressions below. The example names are highlighted in blue, while the rest of the CEL expression is in red.

### Core operators

- **Equality/inequality**: <code><span class="target-signal-name">s.a</span></code> `==` <code><span class="target-signal-name">s.b</span></code>, <code><span class="target-signal-name">s.a</span></code> `!=` <code><span class="target-signal-name">s.b</span></code>
- **Comparisons**: <code><span class="target-signal-name">p.price</span></code> ` > 0`, <code><span class="target-signal-name">p.score</span></code> `>= 90`, <code><span class="target-signal-name">p.cost</span></code> `< 1000`
- **Boolean checks**: <code><span class="target-signal-name">p.flag_active</span> == true</code>, <code><span class="target-signal-name">p.is_valid</span> != false</code>
- **Logical**: `cond1 && cond2`, `cond1 || cond2`, `!cond`
- **Membership**: <code><span class="target-signal-name">p.country</span></code> `in ['US', 'CA']`, <code><span class="target-signal-name">p.role</span></code> `not in ['guest']`
- **Null/empty checks**: <code><span class="target-signal-name">p.some_field</span></code> ` == null`, `size(`<code><span class="target-signal-name">p.some_items</span></code>`) == 0`
- **String contains/starts/ends**: <code><span class="target-signal-name">p.my_text</span></code>`.contains('error')`, <code><span class="target-signal-name">p.my_text</span></code>`.startsWith('ID-')`, <code><span class="target-signal-name">p.my_text</span></code>`.endsWith('.json')`
- **Regex**: <code><span class="target-signal-name">p.my_text</span></code>`.matches('^ID-[0-9]+$')`
- **Length**: `size(` <code><span class="target-signal-name">p.my_text</span></code>`) <= 140`, `size(` <code><span class="target-signal-name">p.my_text</span></code>`) > 0`
- **Numeric tolerance**: `abs(` <code><span class="target-signal-name">s.my_measured_value</span></code>`-` <code><span class="target-signal-name">s.my_actual_value</span></code>`) < 0.01`

### Collections

- **Any/All**: <code><span class="target-signal-name">p.my_items</span></code>`.exists(i, i.status == 'ok')`, <code><span class="target-signal-name">p.my_items</span></code>`.all(i, i.score >= 80)`
- **Contains element**: `['value_A', 'value_B'].exists(f, f == ` <code><span class="target-signal-name">p.my_value</span></code>`)`
- **Subset/superset**: `expected.all(e, e in provided)`

### Dates and numbers

- **Comparing timestamps**: <code><span class="target-signal-name">p.my_time_value</span></code> `< timestamp('2024-12-31T23:59:59Z')`
- **Between**: <code><span class="target-signal-name">p.my_value</span></code> `> 10 && ` <code><span class="target-signal-name">p.my_value</span></code> `< 20`

### Examples

- Require a positive payload value: <code><span class="target-signal-name">p.price</span></code> ` > 0`
- Require a boolean payload value to be true: <code><span class="target-signal-name">p.my_bool_in</span></code> ` == true`
- Ensure an output list contains a value: <code><span class="target-signal-name">output.my_value_objects_list</span></code> `.exists(o, o.`<code><span class="target-signal-name">some_value</span></code>` != null)`
- Guard tolerance on an output: `abs(` <code><span class="target-signal-name">output.my_sensor_reading</span></code> ` - 120.0) < 0.05`
- Limit a payload description length: `size(` <code><span class="target-signal-name">p.my_description</span></code>`) <= 500`

### Working with XML data

When your submission is XML, all element text values arrive in CEL as **strings** — even when they look numeric in the document. This is standard XML behaviour (XML has no native number type). To compare numerically, wrap the value with `double()` or `int()`:

- **Numeric comparison**: `double(`<code><span class="target-signal-name">p.price</span></code>`) > 0` rather than <code><span class="target-signal-name">p.price</span></code> `> 0`
- **Integer check**: `int(`<code><span class="target-signal-name">p.count</span></code>`) >= 1`
- **Collection with cast**: <code><span class="target-signal-name">p.items</span></code>`.all(i, double(i.value) > 0.0)`
- **String comparisons work directly**: <code><span class="target-signal-name">p.status</span></code> `== "active"` (no cast needed)

**XML attributes** (like `<Material Conductivity="160.0">`) become `@`-prefixed keys in the data — `@Conductivity`, not `Conductivity`. Use bracket notation to access them:

- **Access an attribute**: <code><span class="target-signal-name">p.Materials</span>.Material.all(m, double(m["@Conductivity"]) > 0.0)</code>
- **String attribute**: <code><span class="target-signal-name">p.Materials</span>.Material.all(m, m["@Name"] != "")</code>

This is because XML distinguishes between child elements (`<Conductivity>160</Conductivity>`) and attributes (`Conductivity="160"`). The `@` prefix preserves that distinction so your expressions are unambiguous.

If an XML element name contains characters that aren't valid identifiers (hyphens, dots, etc.), access it via bracket notation on `payload`: `payload["THERM-XML"].Materials`.

### Working with named-element data (SysML v2, FHIR, etc.)

Some data formats store values in arrays of named objects rather than as simple key-value pairs. For example, a SysML v2 model might look like:

```json
{
  "ownedAttribute": [
    {"name": "emissivity", "defaultValue": 0.85},
    {"name": "mass", "defaultValue": 3.6}
  ]
}
```

You can't reference `emissivity` directly in a CEL expression because it's a **value**, not a key. The solution is to use **signal mapping** with filter expressions in the data path:

1. Create a signal named `emissivity` in the workflow's signal mapping
2. Set its data path to `ownedAttribute[?@.name=='emissivity'].defaultValue`
3. Write your CEL assertion as `s.emissivity > 0.0 && s.emissivity <= 1.0`

Validibot resolves the filter expression to find the right array element, then makes the value available under the signal name. Your CEL assertions stay clean and readable -- the complexity of navigating the data structure is handled by the data path, not the expression.

See the [Signals](/app/help/validators/signals/) guide for a worked example, and the [Data Paths](/app/help/validators/data-paths/) guide for filter expression syntax.

### Tips

- Expressions run against the submission payload, signals, and validator outputs.
- Keep them deterministic -- no network or external state.
- Use step assertions to tighten a workflow; default assertions always run for the validator.
- Use the namespace prefix (`p.`, `s.`, `output.`) to make it clear where your data comes from. In the UI we color the target portion to help you distinguish it from the rest of the expression.

For more syntax details, visit the CEL specification at <https://github.com/google/cel-spec>.

### Full CEL Expression List

The following CEL statements are supported in Validibot

#### Base CEL Syntax

| Syntax name                     | Description                                                  | Example                                                                               |
| ------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------- | --- | ---------------- |
| Equality / inequality           | Compare two values for equality or difference.               | `p.my_status == "ready"`, `p.my_status != "ok"`                                           |
| Comparisons                     | Numerical comparisons with greater/less operators.           | `p.price > 0`, `p.score >= 90`, `p.cost < 1000`                                             |
| Arithmetic                      | Basic math operators over numbers.                           | `(output.kwh_total - s.kwh_baseline) / s.kwh_baseline < 0.1`                                     |
| Logical                         | Combine boolean expressions.                                 | `cond1 && cond2`, `cond1                                                              |     | cond2`, `!cond1` |
| Membership                      | Test whether a value is inside a list.                       | `p.my_status in ["draft", "approved"]`, `!(p.my_status in ["archived"])`                  |
| Null / empty checks             | Detect missing or empty values.                              | `p.my_field == null`, `size(p.my_items) == 0`                                             |
| JSON-style path access          | Traverse objects and arrays with dot and `[index]` notation. | `p.device[0].id == "abc123"`                                                    |
| Length / size                   | Count characters or list elements.                           | `size(p.my_text) <= 140`, `size(p.my_items) > 0`                                          |
| String contains / starts / ends | String search helpers from CEL stdlib.                       | `p.my_text.contains("error")`, `p.my_text.startsWith("ID-")`, `p.my_text.endsWith(".json")` |
| Regex match                     | Match strings with a regular expression.                     | `p.my_text.matches("^ID-[0-9]+$")`                                                      |
| Collections (exists / all)      | Quantify over list elements.                                 | `p.my_items.exists(i, i.status == "ok")`, `p.my_items.all(i, i.score >= 80)`              |
| Subset / superset check         | Verify one list is contained in another.                     | `s.expected.all(e, e in s.provided)`                                                      |
| Ternary conditional             | Choose a value based on a condition.                         | `p.is_valid ? "pass" : "fail"`                                                          |
| Timestamp comparison            | Compare datetimes via CEL `timestamp()`.                     | `p.event_time < timestamp("2024-12-31T23:59:59Z")`                                      |
| Range / between                 | Combine comparisons to enforce bounds.                       | `p.my_value > 10 && p.my_value < 20`                                                      |

#### Validibot Helpers

| Syntax name                   | Description                                              | Example                                 |
| ----------------------------- | -------------------------------------------------------- | --------------------------------------- |
| `has(value)`                  | True when the value is not null or empty.                | `has(my_description)`                   |
| `is_int(value)`               | True when the numeric value is an integer.               | `is_int(my_floor_area_m2)`              |
| `percentile(values, q)`       | q-quantile of a numeric list (ignores nulls).            | `percentile(my_values, 0.95) < 32.0`    |
| `mean(values)`                | Average of a numeric list (ignores nulls).               | `mean(my_values) <= 50000`              |
| `sum(values)`                 | Sum of a numeric list.                                   | `sum(my_values) > 0`                    |
| `max(values)`                 | Maximum value in a numeric list.                         | `max(my_values) < 75000`                |
| `min(values)`                 | Minimum value in a numeric list.                         | `min(my_values) >= 0`                   |
| `abs(value)`                  | Absolute value of a number.                              | `abs(my_measured - my_expected) < 0.05` |
| `round(value, digits)`        | Round a number to a set of decimal places.               | `round(my_eui_kbtu_ft2, 1) < 30.5`      |
| `duration(series, predicate)` | Count samples where a predicate over the series is true. | `duration(my_series, v > 0) > 100`      |

For `duration`, write the second argument as the condition that should hold for each sample in the series.
