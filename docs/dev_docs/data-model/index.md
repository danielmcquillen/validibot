# Data Model Overview

Validibot's data model is designed around a clear hierarchy: organizations own projects, projects contain workflows, and workflows define a series of steps to either perform a validation on the user's submission or perform an action (e.g. send a Slack message). This page gives you the big picture before diving into individual entities.

---

## Entity Hierarchy

```
Organization
├── Project (namespace for workflows)
│   └── Workflow (ordered validation steps)
│       └── WorkflowStep (one validator or action)
│           └── Validator (the validation class)
│               └── Ruleset (optional schema/rules)
└── Members (users with roles)
```

When a user submits data:

```
Submission (the data to validate)
└── ValidationRun (one execution of a workflow)
    ├── Artifact (files produced by validators)
    ├── ValidationRunSummary (aggregate counts)
    └── ValidationStepRun (result of each step)
        └── ValidationFinding (individual issues found)
```

---

## Core Entities

| Entity                | Purpose                                                                                                      |
| --------------------- | ------------------------------------------------------------------------------------------------------------ |
| **Organization**      | Top-level tenant. All resources belong to an organization.                                                   |
| **Project**           | Namespace within an org. Groups related workflows for organization and reporting.                            |
| **Workflow**          | Ordered sequence of validation steps. Can be active, inactive, archived, or tombstoned for historical-only retention. |
| **WorkflowStep**      | One step in a workflow. Points to a validator (or action) with optional configuration.                       |
| **Validator**         | The validation class (JSON Schema, XML Schema, EnergyPlus, etc.). Defines signals and supported file types. |
| **Ruleset**           | Optional schema or rule file attached to a validator.                                                        |
| **Submission**        | The content being validated (file upload or inline text).                                                    |
| **ValidationRun**     | One execution of a submission through a workflow. Tracks status and timing.                                  |
| **ValidationStepRun** | Execution record for a single step within a run.                                                             |
| **ValidationFinding** | A single issue, warning, or info message from validation.                                                    |
| **Artifact**          | A file produced during validation (reports, logs, transformed data).                                         |
| **ValidationRunSummary** | Aggregate counts that persist after findings are purged.                                                   |

---

## Key Relationships

**Tenancy**: Everything flows down from Organization. A user's access is determined by their membership and role in an organization.

**Workflows and Steps**: Workflows own their steps. Steps are ordered (by `order` field) and execute sequentially. Each step points to exactly one validator.

**Submissions and Runs**: A Submission is immutable content. A ValidationRun links a Submission to a Workflow and tracks the execution. Multiple runs can exist for the same submission (re-runs, different workflows).

**Results**: Each finding belongs to a ValidationStepRun and is denormalized to also reference the parent ValidationRun for query efficiency. Artifacts (files produced by validators) belong to the ValidationRun. Summaries aggregate finding counts and persist after detailed results are purged.

---

## Status and Lifecycle

**Workflow states** (boolean fields, not an enum):

- **Active** (`is_active=True`) — Accepts new validation runs
- **Inactive** (`is_active=False`) — Visible but doesn't accept runs
- **Locked** (`is_locked=True`) — Cannot be edited
- **Archived** (`is_archived=True`) — Hidden by default, preserves history
- **Tombstoned** (`is_tombstoned=True`) — Removed from normal product surfaces while preserving historical runs and credential references

**Run statuses:**

- `PENDING` → `RUNNING` → `SUCCEEDED` | `FAILED` | `CANCELED` | `TIMED_OUT`

**Finding severities:**

- `ERROR` — Blocks the run from passing
- `WARNING` — Non-blocking issue that should be reviewed
- `INFO` — Purely informational
- `SUCCESS` — Assertion passed (positive feedback)

---

## Learn More

Dive deeper into each entity:

- **[Projects](projects.md)** — Namespacing and propagation rules
- **[Submissions](submissions.md)** — Content storage and types
- **[Runs](runs.md)** — Execution tracking and status
- **[Steps](steps.md)** — Step configuration and ordering
- **[Results](results.md)** — Findings, artifacts, and summaries
- **[Signals](signals.md)** — Input, output, and derived signals across steps
- **[Signals Tutorial Example](signals-tutorial-example.md)** — Concrete example of validator-owned signals, step-owned signals, bindings, derivations, and traces
- **[Users & Roles](users_roles.md)** — Membership and permissions
- **[Deletions](deletions.md)** — Soft deletes and cascade behavior
