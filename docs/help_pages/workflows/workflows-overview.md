# Workflows Overview

Workflows define what validation steps your data runs through and in what order.

---

## Creating a Workflow

1. Go to **Workflows** → **New Workflow**
2. Choose a **project** to organize the workflow
3. Give it a descriptive **name**
4. Select the **file types** this workflow accepts
5. Add one or more **steps**

Each step uses a validator to check your data. Add steps in the order you want them to run.

---

## Editing a Workflow

From the workflow detail page, you can:

- **Add, remove, or reorder steps**
- **Change validator settings** on each step
- **Add custom assertions** for stricter validation
- **Update the name, description, or file types**

Inactive workflows show **View** instead of **Edit**. Activate the workflow to enable editing.

Every workflow page shows a small version label near the workflow name. Use the
version selector on the workflow detail page to move between earlier and newer
versions in the same workflow family.

---

## History Policy

Each workflow has a **history policy** that controls what happens after it has validation runs.

| Policy | What it means |
|--------|---------------|
| **Versioned history** | Recommended. Once the workflow has runs, changes that would alter what the workflow validates should be made in a new workflow version. Old runs stay tied to the version that produced them. |
| **Mutable history** | Allows in-place edits after runs. This is useful for experiments and personal drafts, but old run results may no longer match the current workflow definition. |

Versioned history is the default for new workflows.

You can change the history policy before the workflow has runs. After a workflow has runs, change history policy by creating a new workflow version. This keeps one workflow row from mixing versioned-history and mutable-history guarantees.

When a versioned workflow already has runs, Validibot still allows safe edits in
place, such as renaming the workflow or adding a new accepted file type. If an
edit would remove part of the existing validation contract, the form explains
that a new version is required and offers **Create version and apply**. That
button creates the new version, applies your submitted settings there, and keeps
existing runs attached to the old version.

Public workflow listings show the latest active version of each workflow family.
Earlier versions remain available to users with access through the version
selector and direct workflow links.

---

## Running a Workflow

Click **Launch** from the workflow card or detail page to:

1. Select your file type (if the workflow accepts multiple)
2. Upload a file or paste content
3. Click **Run**

Launching only works when the workflow is **active** and not archived.

---

## Workflow States

| State | Meaning |
|-------|---------|
| **Active** | Accepts new validation runs |
| **Inactive** | Visible but won't accept runs (use while editing) |
| **Archived** | Hidden by default, preserves all history |

---

## Archiving Workflows

Archiving disables a workflow without deleting its run history.

**Who can archive:**

- **Owners and Admins** — Can archive/unarchive any workflow
- **Authors** — Can archive/unarchive workflows they created
- **Executors and Viewers** — Cannot archive

**To archive:** Open the workflow and select **Archive** from the actions menu.

**To unarchive:** Enable "Show Archived" in the workflow list, find the workflow, and click **Unarchive**. The workflow returns to inactive status.

---

## Tips

- **Test before activating**: Keep workflows inactive while setting them up
- **Use descriptive names**: "Q4 Compliance Check" is better than "Test Workflow"
- **Archive instead of delete**: You'll keep the audit trail
