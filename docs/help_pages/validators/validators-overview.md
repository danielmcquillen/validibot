# Validators Overview

Validators are the components that check your data. Each validator step uses one validator.

---

## Built-in Validators

| Validator | What it does | File types |
|-----------|--------------|------------|
| **JSON Schema** | Validates JSON structure against a schema | JSON |
| **XML Schema** | Validates XML against an XSD | XML |
| **[Tabular](tabular-validator.md)** | Validates rows against a Table Schema (column types, ranges, uniqueness) plus cross-field CEL row rules | CSV |
| **Basic** | Custom rules using CEL expressions | JSON, YAML |
| **AI** | Natural language validation rules | Any text |

The [Tabular Validator](tabular-validator.md) page explains the column schema it
expects (a Frictionless Table Schema) and the two ways to provide it.

Advanced validators (available with Pro):

| Validator | What it does | File types |
|-----------|--------------|------------|
| **EnergyPlus™** | Runs building energy simulations | IDF, epJSON |
| **FMU** | Runs FMU simulations | FMU packages |

## Fast response or long-running

Container-based step editors (EnergyPlus, FMU, SHACL, and Schematron) include
one **Execution profile** choice:

- **Fast response** is the default. Choose it for interactive checks that you
  expect to finish within about 25 minutes.
- **Long-running** starts more slowly but gives large files and simulations the
  deployment's full validator time allowance.

Choose the profile when you configure the workflow step. Submitters do not need
to choose it for every run, and a run never changes profile after it starts.
Validibot maps the profile to the available compute, so workflow authors do not
need to know about cloud-provider Jobs, Services, queues, or rollback routes.

Validators can declare value or artifact inputs and outputs. Those ports form
the step contract; they are not automatically workflow signals. See
[How Data Flows Through a Workflow](/app/help/concepts/workflow-data/) for the
complete distinction.

---

## Assertions

Validators use **assertions** to check your data:

### Default Assertions
Built into the validator. These always run whenever the validator is used. View them by clicking **View rules** on any step.

### Step Assertions
Custom rules you add to a specific workflow step. Use these to tighten validation for a particular workflow. Step assertions use [CEL expressions](/app/help/concepts/cel-expressions/).

---

## File Type Compatibility

When adding a step, the validator selector shows:

- **Enabled validators**: Compatible with your workflow's file types
- **Disabled validators**: Not compatible (shown with a hint explaining why)

Make sure your workflow's allowed file types match what you want to validate.

---

## Custom Validators

Authors can create custom validators by:

1. Cloning an existing validator template
2. Adding custom default assertions
3. Uploading a schema file (for schema validators)

**Who can edit:**

- Authors can edit validators they created
- Admins and Owners can edit any validator

---

## Tips

- **Keep validators stable**: Use a new integer version rather than changing defaults if you need breaking changes
- **Use default assertions wisely**: They run on every workflow using this validator
- **Check compatibility**: Verify your validator supports the file types your workflow accepts
