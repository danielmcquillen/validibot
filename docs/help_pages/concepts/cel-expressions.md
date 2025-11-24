We use Common Expression Language (CEL) for advanced assertions. Use these patterns to combine the built-in default assertions with step-specific checks.

> **Target signals**
>
> - Unprefixed names resolve to **input** signals by default.
> - **Output** signals can be referenced without a prefix unless an input shares the same name; in that case use `output.<name>`.
> - In the UI, target signals are tinted blue so you can spot the portion of the expression that refers to a signal.

<table>
  <tr>
    <th>Signal</th><th>Example</th><th>Meaning</th>
  </tr>
  <tr>
    <td><code><span class="target-signal-name">price</span></code></td>
    <td><code><span class="target-signal-name">price</span> &gt; 0</code></td>
    <td>Input signal</td>
  </tr>
  <tr>
    <td><code><span class="target-signal-name">output.sensor</span></code></td>
    <td><code><span class="target-signal-name">output.sensor</span> != null</code></td>
    <td>Output signal</td>
  </tr>
</table>

**What you can reference**: only the input signals and output signals that belong to the current validator. Submission metadata (like file type) is not available inside CEL.

### Core operators

- **Equality/inequality**: `a == b`, `a != b`
- **Comparisons**: `price > 0`, `score >= 90`, <span class="target-signal-name">`cost`</span> `< 1000`
- **Boolean checks**: <code><span class="target-signal-name">flag_active</span> == true</code>, <code><span class="target-signal-name">is_valid</span> != false</code>
- **Logical**: `cond1 && cond2`, `cond1 || cond2`, `!cond`
- **Membership**: `country in ['US', 'CA']`, `role not in ['guest']`
- **Null/empty checks**: `field == null`, `size(items) == 0`
- **String contains/starts/ends**: `text.contains('error')`, `text.startsWith('ID-')`, `text.endsWith('.json')`
- **Regex**: `text.matches('^ID-[0-9]+$')`
- **Length**: `size(text) <= 140`, `size(files) > 0`
- **Numeric tolerance**: `abs(measured - target) < 0.01`

### Collections

- **Any/All**: `items.exists(i, i.status == 'ok')`, `items.all(i, i.score >= 80)`
- **Contains element**: `['json', 'xml'].exists(f, f == file_type)`
- **Subset/superset**: `expected.all(e, e in provided)`

### Dates and numbers

- **Comparing timestamps**: `request.received < timestamp('2024-12-31T23:59:59Z')`
- **Between**: `value > 10 && value < 20`

### Examples

- Require a positive input signal: `<span class="target-signal-name">price</span> > 0`
- Require a boolean input to be true: `<span class="target-signal-name">bool_in</span> == true`
- Ensure an output signal exists: `<span class="target-signal-name">outputs</span>.exists(o, o.value != null)`
- Guard tolerance on an output: `abs(output.sensor_reading - 120.0) < 0.05`
- Limit an input description length: `size(description) <= 500`

### Tips

- Expressions run against the submission payload or validator-provided metadata.
- Keep them deterministic—no network or external state.
- Use step assertions to tighten a workflow; default assertions always run for the validator.
- Target names (the signal you point at) appear as `<name>` or `output.<name>`. In the UI we color the target portion to help you distinguish it from the rest of the expression.

For more syntax details, visit the CEL specification at <https://github.com/google/cel-spec>.
