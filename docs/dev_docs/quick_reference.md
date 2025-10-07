# SimpleValidations Quick Reference

SimpleValidations lets you run validations on submitted content through configurable workflows.

## Quick Reference

### Core Concepts

- **Workflow**: an ordered set of validation steps owned by an organization.
- **Submission**: content to validate (either inline text or an uploaded file).
- **WorkflowStep**: one step in a workflow. Each step will have one type of validation defined.
- **ValidationRun**: one execution of a submission through a workflow.
- **ValidationStepRun**: the execution of a single workflow step within a workflow validation run.
- **Validation Finding**: normalized issues/warnings/info produced by steps.

### Basic Usage Flow

1. Create a Workflow in the UI and enable its steps.
2. Start a run by POSTing content to the workflow's start endpoint.
3. The system creates a Submission, enqueues a Validation Run, and returns:
   - 201 Created with the completed run (if it finished quickly), or
   - 202 Accepted with a Location you can poll until completion.
4. Retrieve runs to see status and results. By default, list endpoints show runs from the last 30 days (use `?all=1` for everything).

### Security and Access

- All API endpoints require authentication.
- Listing Workflows shows only those you can access across your orgs.
- Starting a run requires the EXECUTOR role in the workflow's organization.

## Related Documentation

- **Detailed Architecture**: [How It Works](overview/how_it_works.md)
- **Data Model**: [Data Model Overview](data-model/index.md)
- **API Usage**: [Using a Workflow via API](how-to/use-workflow.md)
