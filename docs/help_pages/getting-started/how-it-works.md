## How SimpleValidations works

SimpleValidations runs uploaded submissions through a workflow made of ordered steps:

- **Submission**: A user uploads a file or payload in an allowed format (JSON, text, XML, etc.).
- **Workflow**: A versioned definition owned by an organization. Each workflow can be active, inactive, or archived.
- **Steps**: Each step invokes a validator or integration. Steps run in order and stop if a blocking failure occurs.
- **Validators**: Built-in or custom logic with default assertions that always run. You can add step-level assertions for advanced validators.
- **Results**: Each run records pass/fail details, messages, and timestamps for audit. Archiving keeps history but blocks new runs.

Keep validators aligned with the submission file types you expect, and use archiving to freeze old workflows without losing run history.
