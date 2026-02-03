# Data Model Overview

Validibot's data model is designed around a clear hierarchy: organizations own projects, projects contain workflows, and workflows define how submissions are validated. This page gives you the big picture before diving into individual entities.

---

## Entity Hierarchy

```
Organization
├── Project (namespace for workflows)
│   └── Workflow (ordered validation steps)
│       └── WorkflowStep (one validator or action)
│           └── Validator (the validation engine)
│               └── Ruleset (optional schema/rules)
└── Members (users with roles)
```

When a user submits data:

```
Submission (the data to validate)
└── ValidationRun (one execution of a workflow)
    └── ValidationStepRun (result of each step)
        └── ValidationFinding (individual issues found)
```

---

## Core Entities

| Entity | Purpose |
|--------|---------|
| **Organization** | Top-level tenant. All resources belong to an organization. |
| **Project** | Namespace within an org. Groups related workflows for organization and reporting. |
| **Workflow** | Ordered sequence of validation steps. Can be active, inactive, or archived. |
| **WorkflowStep** | One step in a workflow. Points to a validator (or action) with optional configuration. |
| **Validator** | The validation engine (JSON Schema, XML Schema, EnergyPlus, etc.). Defines signals and supported file types. |
| **Ruleset** | Optional schema or rule file attached to a validator. |
| **Submission** | The content being validated (file upload or inline text). |
| **ValidationRun** | One execution of a submission through a workflow. Tracks status and timing. |
| **ValidationStepRun** | Execution record for a single step within a run. |
| **ValidationFinding** | A single issue, warning, or info message from validation. |

---

## Key Relationships

**Tenancy**: Everything flows down from Organization. A user's access is determined by their membership and role in an organization.

**Workflows and Steps**: Workflows own their steps. Steps are ordered (by `order` field) and execute sequentially. Each step points to exactly one validator.

**Submissions and Runs**: A Submission is immutable content. A ValidationRun links a Submission to a Workflow and tracks the execution. Multiple runs can exist for the same submission (re-runs, different workflows).

**Findings**: Each finding belongs to a ValidationStepRun and is denormalized to also reference the parent ValidationRun for query efficiency.

---

## Status and Lifecycle

**Workflow states:**

- `active` — Accepts new validation runs
- `inactive` — Visible but doesn't accept runs
- `archived` — Hidden by default, preserves history

**Run statuses:**

- `PENDING` → `RUNNING` → `SUCCEEDED` | `FAILED` | `CANCELED` | `TIMED_OUT`

**Finding severities:**

- `ERROR` — Blocks the run from passing
- `WARNING` — Informational, doesn't block
- `INFO` — Purely informational

---

## Learn More

Dive deeper into each entity:

- **[Projects](projects.md)** — Namespacing and propagation rules
- **[Submissions](submissions.md)** — Content storage and types
- **[Runs](runs.md)** — Execution tracking and status
- **[Steps](steps.md)** — Step configuration and ordering
- **[Findings](findings.md)** — Issue structure and aggregation
- **[Users & Roles](users_roles.md)** — Membership and permissions
- **[Deletions](deletions.md)** — Soft deletes and cascade behavior
