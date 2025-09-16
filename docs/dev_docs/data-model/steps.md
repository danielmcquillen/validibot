# Validation Step Runs

A **Validation Step Run** is the execution of one workflow step.  
Each step run belongs to exactly one Validation Run.

It records:

- Which validator/ruleset was applied.
- Status (`pending`, `running`, `passed`, `failed`, `skipped`).
- Timestamps and duration.
- Machine-readable output and error messages.
- Links to any **findings** produced during the step.
