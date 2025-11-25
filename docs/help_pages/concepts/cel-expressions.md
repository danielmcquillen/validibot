SimpleValidations uses Common Expression Language (CEL) for advanced assertions. CEL is a simple, safe expression syntax for writing small, fast, readable conditions and rules over your data.

You can use CEL to perform simple assertion logic on your incoming data,
or data produced by your validator (e.g. after FMI Validator runs a submission through a simulation and produces output).
Whenever you add an assertion to your workflow step, you can base it on a CEL expression.

When the user submits data, each assertion runs. If the assertion has a CEL, and the CEL evaluates to false, the error message you provided in the
assertion will be added to the messages returned to the user.

**What data can you reference?**

In SimpleValidations, there are two types of data 'names' you can reference in a CEL expression. These names let you reference
the data you want to validate:

- **Signals**: a signal name defined by the current validator. These are the names the validator expects to be present in the data.
- **Data paths**: direct paths to input or output data (json notation), if direct data paths are supported by the current validator.

Therefore, the names available for a CEL expression are determined by the signals defined for the validator you've selected and whether the validator allows direct data paths.

**Why not just use JSON or XML Schemas?**

Yes, it's true, you can use the SimpleValidations JSON Schema validator or XML Schema validator for your workflow steps. In this case you don't
define individual rules, you just create one big schema and attach it to your validator. Schemas are great for structure: making sure fields exist, have the right type, follow enums, match patterns, etc. They’re the first line of defense for data integrity.

CEL expressions, on the other hand, handle the behavioural and cross-field rules for data that schemas either can’t express cleanly or make horribly verbose — things like numeric relationships between fields, tolerances, conditional requirements, or checks on simulation outputs from an FMI Validator. In SimpleValidations, schemas define "what the data looks like"; CEL assertions define "what must be true about this data for it to be acceptable." They’re complementary, not competing.

You could create a workflow that has both a JSON schema validation and then some assertions that use detailed CEL expressions.

**Examples**

Here are some examples of CEL expressions below. The example names are highlighted in blue, while the rest of the CEL expression is in red.

### Core operators

- **Equality/inequality**: <code><span class="target-signal-name">a</span></code> `==` <code><span class="target-signal-name">b</span></code>, <code><span class="target-signal-name">a</span></code> `!=` <code><span class="target-signal-name">b</span></code>
- **Comparisons**: <code><span class="target-signal-name">price</span></code> ` > 0`, <code><span class="target-signal-name">score</span></code> `>= 90`, <code><span class="target-signal-name">cost</span></code> `< 1000`
- **Boolean checks**: <code><span class="target-signal-name">flag_active</span> == true</code>, <code><span class="target-signal-name">is_valid</span> != false</code>
- **Logical**: `cond1 && cond2`, `cond1 || cond2`, `!cond`
- **Membership**: <code><span class="target-signal-name">country</span></code> `in ['US', 'CA']`, <code><span class="target-signal-name">role</span></code> `not in ['guest']`
- **Null/empty checks**: <code><span class="target-signal-name">some_field</span></code> ` == null`, `size(`<code><span class="target-signal-name">some_items</span></code>`) == 0`
- **String contains/starts/ends**: <code><span class="target-signal-name">my_text</span></code>`.contains('error')`, <code><span class="target-signal-name">my_text</span></code>`.startsWith('ID-')`, <code><span class="target-signal-name">my_text</span></code>`.endsWith('.json')`
- **Regex**: <code><span class="target-signal-name">my_text</span></code>`.matches('^ID-[0-9]+$')`
- **Length**: `size(` <code><span class="target-signal-name">my_text</span></code>`) <= 140`, `size(` <code><span class="target-signal-name">my_text</span></code>`) > 0`
- **Numeric tolerance**: `abs(` <code><span class="target-signal-name">my_measured_value</span></code>`-` <code><span class="target-signal-name">my_actual_value</span></code>`) < 0.01`

### Collections

- **Any/All**: <code><span class="target-signal-name">my_items</span></code>`.exists(i, i.status == 'ok')`, <code><span class="target-signal-name">my_items</span></code>`.all(i, i.score >= 80)`
- **Contains element**: `['value_A', 'value_B'].exists(f, f == ` <code><span class="target-signal-name">my_value</span></code>`)`
- **Subset/superset**: `expected.all(e, e in provided)`

### Dates and numbers

- **Comparing timestamps**: <code><span class="target-signal-name">my_time_value</span></code> `< timestamp('2024-12-31T23:59:59Z')`
- **Between**: <code><span class="target-signal-name">my_value</span></code> `> 10 && ` <code><span class="target-signal-name">my_value</span></code> `< 20`

### Examples

- Require a positive input signal: <code><span class="target-signal-name">price</span></code> ` > 0`
- Require a boolean input to be true: <code><span class="target-signal-name">my_bool_in</span></code> ` == true`
- Ensure an output signal exists: <code><span class="target-signal-name">my_value_objects_list</span></code> `.exists(o, o.`<code><span class="target-signal-name">some_value</span></code>` != null)`
- Guard tolerance on an output: `abs(` <code><span class="target-signal-name">my_sensor_reading</span></code> ` - 120.0) < 0.05`
- Limit an input description length: `size(` <code><span class="target-signal-name">my_description</span></code>`) <= 500`

### Tips

- Expressions run against the submission payload or validator-provided metadata.
- Keep them deterministic—no network or external state.
- Use step assertions to tighten a workflow; default assertions always run for the validator.
- Target names (the signal you point at) appear as `<name>` or `output.<name>`. In the UI we color the target portion to help you distinguish it from the rest of the expression.

For more syntax details, visit the CEL specification at <https://github.com/google/cel-spec>.

### Full CEL Expression List

The following CEL statements are supported in SimpleValidations

#### Base CEL Syntax

| Syntax name                     | Description                                                  | Example                                                                               |
| ------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------- | --- | ---------------- |
| Equality / inequality           | Compare two values for equality or difference.               | `my_status == "ready"`, `my_status != "ok"`                                           |
| Comparisons                     | Numerical comparisons with greater/less operators.           | `price > 0`, `score >= 90`, `cost < 1000`                                             |
| Arithmetic                      | Basic math operators over numbers.                           | `(kwh_total - kwh_baseline) / kwh_baseline < 0.1`                                     |
| Logical                         | Combine boolean expressions.                                 | `cond1 && cond2`, `cond1                                                              |     | cond2`, `!cond1` |
| Membership                      | Test whether a value is inside a list.                       | `my_status in ["draft", "approved"]`, `!(my_status in ["archived"])`                  |
| Null / empty checks             | Detect missing or empty values.                              | `my_field == null`, `size(my_items) == 0`                                             |
| JSON-style path access          | Traverse objects and arrays with dot and `[index]` notation. | `payload.device[0].id == "abc123"`                                                    |
| Length / size                   | Count characters or list elements.                           | `size(my_text) <= 140`, `size(my_items) > 0`                                          |
| String contains / starts / ends | String search helpers from CEL stdlib.                       | `my_text.contains("error")`, `my_text.startsWith("ID-")`, `my_text.endsWith(".json")` |
| Regex match                     | Match strings with a regular expression.                     | `my_text.matches("^ID-[0-9]+$")`                                                      |
| Collections (exists / all)      | Quantify over list elements.                                 | `my_items.exists(i, i.status == "ok")`, `my_items.all(i, i.score >= 80)`              |
| Subset / superset check         | Verify one list is contained in another.                     | `expected.all(e, e in provided)`                                                      |
| Ternary conditional             | Choose a value based on a condition.                         | `is_valid ? "pass" : "fail"`                                                          |
| Timestamp comparison            | Compare datetimes via CEL `timestamp()`.                     | `event_time < timestamp("2024-12-31T23:59:59Z")`                                      |
| Range / between                 | Combine comparisons to enforce bounds.                       | `my_value > 10 && my_value < 20`                                                      |

#### SimpleValidation Helpers

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
