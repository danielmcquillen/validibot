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
