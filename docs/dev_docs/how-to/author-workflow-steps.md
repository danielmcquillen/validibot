# Authoring Workflow Steps

This guide walks through the two-stage wizard used to add or edit workflow steps in SimpleValidations.

## 1. Choose the validation type

1. Open a workflow (either create a new workflow or open an existing one) and click **Add step**.
2. A modal displays every `Validator` grouped by `validation_type`. Each option shows:
   - The validator name and type.
   - An info tooltip with the validator description (if provided).
3. Select the validator you want to use and press **Continue**. The modal swaps to the configuration form for that type.

## 2. Configure the validation

The configuration form is specific to the validation type you picked. All forms include a **Step name** field.

### JSON Schema
- Pick whether to **paste** the schema or **upload** a file.
- Pasting text saves the schema into a `Ruleset`; a short preview is stored with the step.
- Uploading replaces any existing schema file associated with the step.

### XML Schema
- Choose the schema flavour (**DTD**, **XSD**, or **RELAXNG**).
- Paste the XSD/RNG/DTD content or upload a file—behaviour mirrors the JSON workflow.
- The schema type is preserved in the associated `Ruleset` metadata.

### EnergyPlus
- Decide whether the step **runs a simulation** or only performs static IDF checks.
- Pick initial IDF checks (duplicate names, autosizing, schedule coverage, etc.).
- Choose post-simulation checks (EUI range, peak load) and define optional EUI minimum/maximum values.
- Add notes to capture any context for the run.

### AI Assist
- Select the template (**AI Critic** or **Policy Check**).
- Add JSONPath selectors to control which parts of the document are sent to the AI engine.
- Define policy rules using the syntax `<path> <operator> <value> | optional message`. Supported operators: `>=`, `>`, `<=`, `<`, `==`, `!=`, `between`, `in`, `not_in`, `nonempty`.
- Pick advisory vs blocking mode and set a per-run cost cap.

After saving, the modal closes and the workflow step list refreshes automatically. Steps are always resequenced with gaps of 10 so you can reorder them later without conflicts.

## Editing or reordering steps

- Click the **Edit** icon on any step to reopen the wizard directly on the configuration form.
- Move steps up or down using the arrow buttons; the system resequences steps atomically to avoid order collisions.
- Deleting a step updates the workflow immediately and reorders the remaining steps.

## Tips for authors

- Keep selectors and policy rules small and focused; each item increases payload size and cost for AI-assisted steps.
- Use descriptive step names—these labels show up on validation run summaries and in the dashboard.
- Run a test submission after adding or editing steps to confirm the new configuration behaves as expected.
