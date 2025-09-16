# Data Model Overview

The core entities in SimpleValidations are:

- **Submission**: a request to validate a file against a workflow.
- **Validation Run (Job)**: one execution of a submission through a workflow.
- **Validation Step Run**: the execution of a single workflow step.
- **Validation Finding**: a normalized issue, warning, or info item produced by a step.

This hierarchy ensures that all validations are reproducible, traceable, and auditable.
