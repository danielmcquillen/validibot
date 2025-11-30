# Data Model Overview

The core entities in Validibot are:

- **Projects** provide an organization-scoped namespace that tags workflows,
  submissions, and runs for tenancy and reporting.
- **Validators** define the engines, signals, and catalogs a workflow step can execute.
- **Ruleset Assertions** capture the concrete checks that a validator runs once inputs and outputs exist.
- **Submission** records the original payload to validate.
- **Validation Run (Job)** tracks one execution of a submission through a workflow.
- **Validation Step Run** records the execution of a single workflow step.
- **Validation Finding** is the normalized result of an assertion or engine failure.

These entities keep validations reproducible, traceable, and auditable. Dive deeper into each topic via
the left-hand navigation.
