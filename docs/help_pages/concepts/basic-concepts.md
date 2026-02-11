# Core Concepts

The key terms you'll encounter in Validibot.

---

## Organization

Your workspace. Each organization has its own workflows, projects, and team members. Users can belong to multiple organizations.

**Roles** control what you can do:

| Role | Can do |
|------|--------|
| **Owner** | Everything, including delete the org |
| **Admin** | Manage members, create/edit/archive any workflow |
| **Author** | Create and edit their own workflows |
| **Executor** | Run workflows and view results |
| **Viewer** | View workflows and results (read-only) |

---

## Project

A folder for organizing related workflows. Every workflow belongs to a project. Use projects to group workflows by team, data type, or purpose.

---

## Workflow

A reusable sequence of validation steps. When you submit data, it runs through each step in order.

**States:**

- **Active** — Accepts new validation runs
- **Inactive** — Visible but won't accept runs (useful while editing)
- **Archived** — Hidden by default, preserves all run history

---

## Step

One action in a workflow. Most steps run a **validator** that checks your data. Steps execute in order from top to bottom.

Some workflows also include **action steps** that do things like send Slack notifications or generate certificates.

---

## Validator

The engine that checks your data. Built-in validators include:

- **JSON Schema** — Validates JSON structure
- **XML Schema** — Validates XML against XSD
- **Basic** — Custom CEL expression rules
- **AI** — Natural language rules

Each validator has **default assertions** that always run. Advanced validators let you add **step-level assertions** for workflow-specific rules.

---

## Assertion

A rule evaluated during validation. Assertions can be:

- **Default assertions** — Built into the validator, always run
- **Step assertions** — Added to a specific workflow step

Assertions use CEL (Common Expression Language) for custom logic. See [CEL Expressions](cel-expressions.md) for syntax.

---

## Submission

The file or content you upload for validation. Validibot supports JSON, XML, YAML, CSV, and plain text. The submission's file type must match what the workflow's validators support.

Submissions are immutable—once uploaded, they don't change.

---

## Run

One execution of a workflow against a submission. Each run tracks:

- **Status**: PENDING → RUNNING → SUCCEEDED/FAILED
- **Findings**: Issues discovered during validation
- **Timing**: When it started, ended, and how long it took

Runs are kept for audit even if you archive or delete the workflow.

---

## Finding

An issue, warning, or note discovered during validation.

**Severity levels:**

| Level | Meaning |
|-------|---------|
| **Error** | A problem that must be fixed. Fails the run. |
| **Warning** | Worth reviewing, but doesn't fail the run. |
| **Info** | Informational note. No effect on pass/fail. |

Each finding includes a message, the location in your data (path), and which step found it.
