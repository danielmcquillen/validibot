# Validation Runs (Jobs)

A **Validation Run** (sometimes just called a "Job") is one execution of a Submission through a workflow.

It records:

- Status (`pending`, `running`, `succeeded`, `failed`, `canceled`, `timed_out`).
- Start and end timestamps.
- Duration.
- Resolved configuration (rulesets, thresholds, overrides).
- A summary of results (e.g. counts of errors/warnings).
- Links to **step runs**, **findings**, and **artifacts**.

Runs provide the durable audit trail of what happened during validation.
