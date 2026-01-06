# Authoring Workflow Steps

This guide walks through the two-stage wizard used to add or edit workflow steps in Validibot.

## Pause or resume a workflow

Owners, Admins, and Authors can pause a workflow whenever you need to stop new validation runs without deleting the configuration. Open the workflow detail page and use the **Disable workflow** button in the Status panel. While inactive, the workflow:

- stays visible in the catalog so teammates can review its setup;
- blocks new runs from both the UI and the `/api/v1/orgs/{org_slug}/workflows/{workflow_identifier}/runs/` endpoint (calls return HTTP 403);
- allows in-flight runs to finish normally.

Re-enable the workflow from the same panel when you are ready to accept submissions again. Executors and Viewers can still open the page, but they will see read-only messaging that the workflow is inactive.

## 1. Choose the validation type

1. Open a workflow (either create a new workflow or open an existing one) and click **Add step**.
2. A modal displays every available option across four tabs:
   - **Validators** (BASIC, JSON Schema, and XML Schema)
   - **Advanced validators** (AI Assist, EnergyPlus, and FMI)
   - **Integrations** (action definitions such as Slack notifications)
   - **Certifications** (action definitions that issue certificates or badges)
   Each card shows the item name, category, icon, and description.
3. Select the validator or action you want to use and press **Continue**. The modal closes and you are redirected to the full-screen editor with breadcrumb navigation (`Workflows > <Workflow> > Step …`).

## 2. Configure the validation

The dedicated editor is specific to the validation type you picked. All forms include a **Step name** field along with convenient navigation at the bottom of the page to jump back to the workflow overview or, when editing, to switch between adjacent steps.

The **Step Assertions** panel always shows a **Default assertions** card at the top. This card summarizes the validator-level default assertions that will run before any step-specific assertions and links to a modal listing the full set; from there you can jump to the validator’s read-only detail page if you need to review the defaults in depth.

### JSON Schema
- Paste the schema or upload a file—the editor detects the source automatically.
- JSON schemas must declare `$schema` as Draft 2020-12; the editor enforces this version automatically.
- Pasting text stores the schema in the ruleset's `rules_text` field; a short preview is stored with the step for quick inspection.
- Uploading saves the schema to `rules_file`, clears any inline text, and overwrites the previous file (uploads are capped at 2&nbsp;MB).

### XML Schema
- Choose the schema flavour (**DTD**, **XSD**, or **RELAXNG**).
- Paste the XSD/RNG/DTD content or upload a file—the editor detects which one you used and stores it in the appropriate ruleset field.
- The selected schema type is persisted on the ruleset metadata (`metadata['schema_type']`).

### EnergyPlus
- Decide whether the step **runs a simulation** or only performs static IDF checks.
- Pick initial IDF checks (duplicate names, autosizing, schedule coverage, etc.).
- Choose post-simulation checks (EUI range, peak load) and define optional EUI minimum/maximum values.
- Add notes to capture any context for the run.

### FMI (preview)
- Attach an FMI validator and upload an FMU. The upload is stored in canonical storage (S3 in production) **and** copied into a Modal Volume cache keyed by the FMU checksum, so Modal runs never need a presigned URL.
- Workflow submissions for FMI steps remain JSON/text; the FMU itself is uploaded once at validator creation. This keeps launch-time payloads simple while the validator uses the stored FMU for simulation.
- Catalog inputs/outputs are generated from the FMU metadata. A **probe** is a short, safety-first run that opens the FMU, validates `modelDescription.xml`, checks for suspicious files, and seeds the catalog before assertions can be added.
- Execution now runs on Modal using the cached FMU: inputs flow to the FMU run, outputs are captured, and CEL assertions evaluate them just like other validators.

### AI Assist
- Select the template (**AI Critic** or **Policy Check**).
- Add JSONPath selectors to control which parts of the document are sent to the AI engine.
- Define policy rules using the syntax `<path> <operator> <value> | optional message`. Supported operators: `>=`, `>`, `<=`, `<`, `==`, `!=`, `between`, `in`, `not_in`, `nonempty`.
- Pick advisory vs blocking mode and set a per-run cost cap.

### Actions (Integrations & Certifications)
- Actions reuse catalogued definitions (for example, sending a Slack message or issuing a signed certificate).
- Slack integrations prompt for the message that will be posted when the step runs; the text is stored on a dedicated `SlackMessageAction` model.
- Certification steps let you upload an optional certificate template. If you skip the upload, Validibot falls back to the bundled `default_signed_certificate.pdf`. Editing the step keeps the previous upload unless you explicitly provide a replacement.
- The editor lets you rename the step, adjust the author notes, and record any action-specific inputs in purpose-built forms instead of the generic JSON payload we used previously.
- Action steps never expose schemas to end users, but they appear alongside validation steps in the workflow timeline and step navigation.

After saving, you are redirected to the workflow detail page and the step list refreshes automatically. Steps are always resequenced with gaps of 10 so you can reorder them later without conflicts.

## Editing or reordering steps

- Click the **Edit** icon on any step to open the full-screen editor. The previous/next step shortcuts at the bottom of the page make it easy to move across complex workflows.
- Move steps up or down using the arrow buttons; the system resequences steps atomically to avoid order collisions.
- Deleting a step updates the workflow immediately and reorders the remaining steps.

## Tips for authors

- Keep selectors and policy rules small and focused; each item increases payload size and cost for AI-assisted steps.
- Use descriptive step names—these labels show up on validation run summaries and in the dashboard.
- Run a test submission after adding or editing steps to confirm the new configuration behaves as expected.
