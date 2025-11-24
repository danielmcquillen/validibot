## Workflows overview

Workflows define the ordered validation steps your submissions run through.

- **Create**: Pick a project, name, and submission types. Add steps in the order they should run.
- **Edit**: Use the workflow detail page to change steps, validator options, or assertions. Inactive workflows show **View** instead of **Edit**.
- **Run**: Launch manually from the workflow card. Launching is allowed only when the workflow is active and not archived.

### Archiving workflows

- Archiving disables the workflow (no new runs) but keeps past ValidationRuns for audit.
- Owners/Admins can archive/unarchive any workflow in their org. Authors can archive/unarchive only workflows they created. Executors/viewers cannot archive.
- Archiving sets the workflow to archived + disabled; unarchiving clears the archived flag and re-enables runs.
- The workflow list lets you show/hide archived items; archived rows/cards expose a View + Unarchive action.
