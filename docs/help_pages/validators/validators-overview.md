# Validators Overview

Validators are the engines that check your data. Each workflow step uses one validator.

---

## Built-in Validators

| Validator | What it does | File types |
|-----------|--------------|------------|
| **JSON Schema** | Validates JSON structure against a schema | JSON |
| **XML Schema** | Validates XML against an XSD | XML |
| **Basic** | Custom rules using CEL expressions | JSON, YAML |
| **AI** | Natural language validation rules | Any text |

Advanced validators (available with Pro):

| Validator | What it does | File types |
|-----------|--------------|------------|
| **EnergyPlus** | Runs building energy simulations | IDF, epJSON |
| **FMU** | Runs FMU simulations | FMU packages |

---

## Assertions

Validators use **assertions** to check your data:

### Default Assertions
Built into the validator. These always run whenever the validator is used. View them by clicking **View rules** on any step.

### Step Assertions
Custom rules you add to a specific workflow step. Use these to tighten validation for a particular workflow. Step assertions use [CEL expressions](../concepts/cel-expressions.md).

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

- **Keep validators stable**: Create a new version rather than changing defaults if you need breaking changes
- **Use default assertions wisely**: They run on every workflow using this validator
- **Check compatibility**: Verify your validator supports the file types your workflow accepts
