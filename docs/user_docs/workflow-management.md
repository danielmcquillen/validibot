# Workflow Management

Workflows are the heart of Validibot. Each workflow defines an ordered sequence of validation steps that your data passes through. This guide covers how to create, organize, edit, and maintain workflows as your validation library grows.

## Planning Your Workflow Library

Before creating workflows, think about how to organize them:

**By data type**: Create separate workflows for different file formats or data structures. A "Building Model Validation" workflow might handle EnergyPlus IDF files, while a "Configuration Check" workflow handles JSON configuration files.

**By validation purpose**: Group validations by what they check. A "Compliance Check" workflow might verify regulatory requirements, while a "Quality Assurance" workflow checks for common data entry errors.

**By audience**: If different teams need different validation levels, create workflows for each. A "Quick Check" workflow might run fast schema validation, while a "Full Analysis" workflow includes simulation-based validators.

### Naming Conventions

Consistent naming helps teammates find the right workflow:

- Include the data type: "IDF Building Model Validation"
- Include the purpose: "ASHRAE 90.1 Compliance Check"
- Consider versioning: "Product Schema v2" (when you need breaking changes)

## Creating a Workflow

To create a new workflow:

1. Navigate to **Workflows** in the sidebar.
2. Click **New Workflow**.
3. Fill in the required fields:
   - **Name**: A clear, descriptive name
   - **Project**: The project this workflow belongs to
   - **Allowed File Types**: Which formats this workflow accepts (JSON, XML, TEXT, etc.)
4. Add optional fields:
   - **Description**: Explain what this workflow validates and when to use it
   - **Public Information**: A description visible to anyone who runs this workflow
5. Click **Create**.

The workflow starts in an inactive state, giving you time to add steps before making it available.

### Cloning a Workflow

To create a variation of an existing workflow:

1. Open the workflow you want to copy.
2. Click **Clone** (or find it in the workflow's action menu).
3. Give the clone a new name.
4. Edit the cloned workflow as needed.

Cloning is useful when you need similar validation logic with minor differences, like checking the same schema with different assertion thresholds.

## Adding and Configuring Steps

Each workflow needs at least one step. Steps execute in order from top to bottom.

### Adding a Step

1. From the workflow detail page, click **Add Step**.
2. Select a **Validator** from the list. The list shows:
   - All validators compatible with your workflow's allowed file types
   - Validators that don't match are shown but disabled with a hint
3. Configure the step:
   - **Name**: A label for this step (e.g., "Schema Validation", "Energy Use Check")
   - **Validator-specific settings**: Vary by validator type (schema files, configuration options, etc.)
4. Click **Save**.

### Reordering Steps

Drag steps to change their execution order. Early steps typically perform basic checks (syntax, schema compliance), while later steps perform deeper analysis.

### Removing Steps

Click the delete icon on a step to remove it. This doesn't affect past validation runs—their results are preserved.

### Validator vs Action Steps

Most steps use **validators** that check your data. Some workflows also include **action steps** that do other things:

- Send notifications (Slack, email)
- Generate certificates or badges
- Trigger external systems

Action steps typically come after validation steps to respond to the validation outcome.

## Editing Workflow Settings

From the workflow detail page, click **Edit** (or the settings icon) to modify:

**Name and Description**: Update these as your workflow evolves.

**Status**: Control whether the workflow accepts new runs:

- **Active**: The workflow accepts validation runs.
- **Inactive**: The workflow is visible but won't accept new runs. Use this while making changes or when temporarily disabling a workflow.

**Project**: Move the workflow to a different project if your organization changes.

**Allowed File Types**: Add or remove supported formats. Changing this may require updating your steps if validators don't support the new types.

**Public Information**: Update the description shown to users who run this workflow.

## Assertions

Validibot evaluates two tiers of assertions for each workflow step. Both
produce findings visible in the run results, and both count toward the step's
assertion statistics.

### Default Assertions

Default assertions are defined by the validator author on the validator itself.
They run automatically whenever the validator executes, regardless of which
workflow step is using it. Validator authors manage them from the validator
detail page.

Default assertions are always evaluated first. Workflow authors cannot override
or remove them — they represent the validator's built-in domain checks (for
example, "site EUI must be positive"). When you view a workflow step in the
editor, a card shows how many default assertions the selected validator has.

### Step Assertions

Step assertions are authored by the workflow creator and are specific to one
workflow step. They let you add custom rules on top of the validator's defaults.

**When to use step assertions:**

- To tighten validation beyond the default checks
- To enforce business rules specific to your use case
- To check relationships between data fields

**Adding step assertions:**

1. Edit the workflow step.
2. In the "Assertions" section, click **Add Assertion**.
3. Define:
   - **Expression**: The CEL expression that must evaluate to true
   - **Message**: The error message shown when the assertion fails
   - **Severity**: Error, Warning, or Info
4. Save the step.

### Evaluation Order

When a step runs, assertions are evaluated in this order:

1. Default assertions from the validator (always run first)
2. Step assertions defined on the workflow step (run second)

Findings from both tiers appear together in the results.

## Workflow Lifecycle

### Active vs Inactive

- **Active** workflows accept new validation runs and appear prominently in the UI.
- **Inactive** workflows are visible but don't accept runs. The "Launch" button is disabled.

Toggle status from the workflow detail page. Making a workflow inactive doesn't affect runs already in progress.

### Archiving Workflows

When a workflow is no longer needed but you want to preserve its history:

1. Open the workflow.
2. Click **Archive** (in the actions menu).

Archived workflows:

- Don't appear in the default workflow list (toggle "Show Archived" to see them)
- Cannot accept new validation runs
- Preserve all historical runs and findings
- Can be unarchived later if needed

**Who can archive?**

- **Owners and Admins** can archive any workflow in their organization.
- **Authors** can archive workflows they created.
- **Executors and Viewers** cannot archive workflows.

### Unarchiving

To restore an archived workflow:

1. Enable "Show Archived" in the workflow list.
2. Find the archived workflow.
3. Click **Unarchive**.

The workflow returns to inactive status. Set it to active when you're ready to accept runs again.

### Deleting Workflows

Deleting a workflow permanently removes it and all associated runs. This action cannot be undone.

Consider archiving instead of deleting when you might need the validation history later.

## Best Practices

**Test before activating**: Keep workflows inactive while you're still setting up and testing. Run sample validations to verify the behavior before making the workflow available to others.

**Document your workflows**: Use the description and public information fields to explain what the workflow checks and when to use it. Future you (and your teammates) will thank you.

**Version with care**: When you need to make breaking changes, consider creating a new workflow (e.g., "Product Schema v2") rather than modifying the existing one. This preserves history and lets users migrate gradually.

**Review validator compatibility**: When changing allowed file types, verify that all steps still work. Validators that don't support the new types will show warnings.

**Use projects consistently**: Keep related workflows in the same project. This makes it easier to find workflows and analyze validation trends across related data.
