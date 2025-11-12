# SimpleValidations Quick Reference

SimpleValidations lets you run validations on submitted content through configurable workflows.

## Quick Reference

### Core Concepts

- **Workflow**: an ordered set of validation steps owned by an organization and scoped to one of its projects.
- **Submission**: content to validate (either inline text or an uploaded file).
- **WorkflowStep**: one step in a workflow. Each step will have one type of validation defined.
- **ValidationRun**: one execution of a submission through a workflow.
- **ValidationStepRun**: the execution of a single workflow step within a workflow validation run.
- **Validation Finding**: normalized issues/warnings/info produced by steps.

### Basic Usage Flow

1. Create a Workflow in the UI, choose its default project, and enable its steps.
2. Start a run by POSTing content to the workflow's start endpoint.
3. The system creates a Submission, enqueues a Validation Run, and returns:
   - 201 Created with the completed run (if it finished quickly), or
   - 202 Accepted with a Location you can poll until completion.
4. Retrieve runs to see status and results. By default, list endpoints show runs from the last 30 days (use `?all=1` for everything).
5. Owners, Admins, and Authors can temporarily disable a workflow when you need to stop new runs; re-enable it from the workflow detail page when ready.
6. Use the **Run** page (`/app/workflows/<workflow id>/launch/`) to submit manual runs. Successful submissions redirect to a dedicated run-detail page that streams updates over HTMX while the validation executes. Both pages require EXECUTOR access.

### Sharing Workflow Details

- Toggle the **Make info public** setting on a workflow to expose its Info tab at `/workflows/<workflow uuid>/info`. Only the overview panel is shared; launching still requires authentication inside the app.
- Visit `/workflows/` to browse all public workflows plus any you can access when signed in. Use the search bar, layout toggle, and pagination controls to find the right workflow quickly.

### Seeding Demo Workflows

- Run `uv run -- python manage.py create_dummy_workflows --count 10` to generate demo-ready workflows (defaults to 10). The command creates sample organizations, projects, public info, and steps with faker content so the public pages look realistic.

### Security and Access

- All API endpoints require authentication.
- Listing Workflows shows only those you can access across your orgs.
- Starting a run requires the EXECUTOR role in the workflow's organization.

## Related Documentation

- **Detailed Architecture**: [How It Works](overview/how_it_works.md)
- **Data Model**: [Data Model Overview](data-model/index.md)
- **API Usage**: [Using a Workflow via API](how-to/use-workflow.md)
