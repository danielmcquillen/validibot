# SimpleValidations Overview

SimpleValidations lets you run validations on submitted content through configurable workflows.

Core concepts

- Workflow: an ordered set of validation steps owned by an organization.
- Submission: content to validate (either inline text or an uploaded file).
- Validation Run: one execution of a submission through a workflow.
- Validation Step Run: the execution of a single step within a run.
- Validation Finding: normalized issues/warnings/info produced by steps.

How it works

1. Create a Workflow in the UI and enable its steps.
2. Start a run by POSTing content to the workflow’s start endpoint.
3. The system creates a Submission, enqueues a Validation Run, and returns:
   - 201 Created with the completed run (if it finished quickly), or
   - 202 Accepted with a Location you can poll until completion.
4. Retrieve runs to see status and results. By default, list endpoints show runs from the last 30 days (use `?all=1` for everything).

Security and access

- All API endpoints require authentication.
- Listing Workflows shows only those you can access across your orgs.
- Starting a run requires the EXECUTOR role in the workflow’s organization.

Related docs

- Data model: ./data-model/index.md
