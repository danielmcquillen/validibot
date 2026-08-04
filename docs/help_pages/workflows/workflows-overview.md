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

To open an older version, use the **Version history** card on the workflow
detail page.

---

## Editing Policy

Each workflow has an **Editing policy** that controls how its definition may
change after it has validation runs. The information icon beside the field
explains both choices and when the choice becomes fixed.

| Policy | What it means |
|--------|---------------|
| **Versioned** | Recommended. Once the workflow has runs or is locked, changes that would alter what the workflow validates should be made in a new workflow version. Old runs stay tied to the version that produced them. |
| **Mutable** | Allows in-place semantic edits after completed runs. This is useful for experiments and personal drafts, but old run results may no longer match the current workflow definition. |

Versioned is selected by default for every new workflow. To use Mutable, the
author must choose it deliberately.

You can change the Editing policy in either direction while the workflow has
no runs and is not locked. After a workflow has runs or is locked, the select
shows its saved value but is disabled with a reason. Create a new workflow
version to use a different policy. This keeps one workflow row from mixing
Versioned and Mutable historical guarantees.

Mutable workflows still cannot be changed underneath a validation that is
using their definition. If a semantic save is rejected because one or more
runs are pending, running, or finalizing, none of that save is applied. Retry
after those runs finish. For a continuously busy workflow, deactivate it to
stop new launches, wait for current runs to finish, save, and reactivate it.
Names, descriptions, and lifecycle settings that do not change what the
workflow validates remain editable during a run.

When a Versioned workflow already has runs, Validibot still allows safe edits in
place, such as renaming the workflow or adding a new accepted file type. If an
edit would remove part of the existing validation contract, the form explains
that a new version is required and offers **Create version and apply**. That
button creates the new version, applies your submitted settings there, and keeps
existing runs attached to the old version.

Public workflow listings show the latest active version of each workflow family.
Earlier versions remain available to users with access through the **Version
history** card on the workflow detail page and exact workflow links.

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
