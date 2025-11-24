## Core concepts

- **Organization**: Your workspace. Roles (Owner, Admin, Author, Executor, Viewer) control what you can do.
- **Workflow**: A reusable, versioned sequence of steps that validates a submission. Can be active, inactive, or archived.
- **Step**: One action in a workflow. Most steps call a validator; order matters.
- **Validator**: Built-in or custom logic that checks the submission. Each validator has default assertions that always run.
- **Assertion**: A rule evaluated during validation. Advanced validators let you add step-level assertions, often using CEL.
- **Submission**: The file or payload provided at launch time. File type and data format must match what the chosen validator supports.
- **Runs**: Executions of a workflow. Runs are retained for audit even if the workflow is archived.
