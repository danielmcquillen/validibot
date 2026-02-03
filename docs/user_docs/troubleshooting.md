# Troubleshooting

This guide helps you diagnose and resolve common issues when using Validibot. If you can't find your issue here, check the [FAQ](faq.md) or contact support.

## Workflow Issues

### "Workflow name already exists"

**Problem**: You're trying to create a workflow but the name is already taken.

**Solution**: Workflow names (and their generated slugs) must be unique within an organization. Either:

- Choose a different name
- Add a version number or suffix (e.g., "Product Validation v2")
- Archive or delete the existing workflow if it's no longer needed

### "Cannot add step: validator not compatible"

**Problem**: You're trying to add a validator that doesn't support your workflow's file types.

**Solution**: Check the workflow's "Allowed File Types" setting. The validator you're adding must support at least one of these types. Either:

- Change the workflow's allowed file types to include formats the validator supports
- Choose a different validator that matches your current file types

### Workflow won't activate

**Problem**: The workflow status won't change to Active.

**Possible causes**:

1. **No steps**: Workflows need at least one validation step before they can be activated
2. **Invalid configuration**: One or more steps may have configuration errors
3. **Missing permissions**: You may not have Author or Admin access

**Solution**: Check that the workflow has steps, review any error messages on the workflow page, and verify your role in the organization.

## Run Failures

### Understanding why a run failed

When a validation run fails, first determine the type of failure:

**Data validation failure**: The run completed, but your data didn't pass the validation rules. Look at the ERROR-level findings to understand what needs to be fixed in your data.

**System error**: Something went wrong during execution. The error message will indicate what happened (timeout, configuration issue, etc.).

**Permission error**: You may not have access to run this workflow, or the workflow may be inactive.

### "Workflow is not active"

**Problem**: You can't launch a run because the workflow is disabled.

**Solution**: Ask a workflow Author or Admin to activate the workflow. In the UI, the Launch button is hidden for inactive workflows.

### "No workflow steps configured"

**Problem**: The workflow exists but has no validation steps.

**Solution**: Someone needs to add at least one step to the workflow before it can accept runs.

### "File type not supported"

**Problem**: The file format you submitted isn't accepted by this workflow.

**Solution**:

1. Check which file types the workflow accepts (visible on the workflow detail page)
2. Verify your file is actually the format you think it is
3. For API submissions, ensure your `Content-Type` header matches the file format

### Run stuck in PENDING

**Problem**: The run was created but never started.

**Possible causes**:

1. **Queue backup**: The task queue may be processing other jobs
2. **Worker issues**: The background worker may be down or overloaded
3. **Resource limits**: The system may have hit resource constraints

**Solution**: Wait a few minutes for the queue to clear. If the run doesn't start within 5-10 minutes, contact your administrator.

### Run timed out

**Problem**: The run exceeded the maximum allowed execution time.

**Possible causes**:

1. **Large payload**: Very large files take longer to process
2. **Complex validation**: Advanced validators (like simulation-based validators) can take significant time
3. **System load**: High system load can slow execution

**Solution**: For legitimate long-running validations, ask your administrator about increasing timeout limits. For large files, consider whether all the data is necessary or if you can validate a subset.

## API Errors

### Understanding error responses

Validibot API errors follow a consistent format:

```json
{
  "detail": "Human-readable error message",
  "code": "machine_readable_code",
  "status": 400,
  "errors": []
}
```

The `code` field is stable and can be used for programmatic error handling. The `detail` field provides context for humans.

### Common API error codes

| Code | HTTP Status | Meaning |
|------|-------------|---------|
| `workflow_inactive` | 409 | Workflow is not active |
| `no_workflow_steps` | 400 | Workflow has no steps |
| `file_type_unsupported` | 400 | File format not accepted |
| `unsupported_media_type` | 415 | Content-Type header mismatch |
| `authentication_required` | 401 | Missing or invalid API token |
| `permission_denied` | 403 | Insufficient permissions |
| `not_found` | 404 | Workflow or resource doesn't exist |
| `rate_limited` | 429 | Too many requests |

### "Authentication required" (401)

**Problem**: Your API request was rejected for authentication.

**Solution**:

1. Verify you're including the `Authorization: Bearer <token>` header
2. Check that your token hasn't expired
3. Ensure the token has access to the target organization

### "Permission denied" (403)

**Problem**: You're authenticated but don't have access.

**Solution**:

1. Verify you have Executor role (or higher) in the organization
2. Check that you're using the correct organization slug in the URL
3. Confirm the API token was created for the right organization

### "Rate limited" (429)

**Problem**: You've made too many requests.

**Solution**: Add delays between requests, implement exponential backoff, or contact your administrator about rate limit adjustments.

## Performance Issues

### Slow validation runs

**Possible causes**:

1. **Large files**: Bigger payloads take longer to process
2. **Many steps**: Each step adds execution time
3. **Complex validators**: Simulation-based validators are inherently slower
4. **System load**: High overall system usage

**Solutions**:

- Trim unnecessary data from submissions
- Consider splitting complex workflows into focused ones
- For advanced validators, ensure inputs are properly formatted to avoid preprocessing delays

### UI is slow or unresponsive

**Possible causes**:

1. **Large result sets**: Runs with thousands of findings can slow rendering
2. **Network issues**: Check your connection
3. **Browser cache**: Try clearing your browser cache

**Solutions**:

- Use filtering to reduce displayed findings
- Try a different browser
- Clear browser cache and reload

## Getting Help

When contacting support, include:

1. **What you were trying to do**: Specific action you attempted
2. **What happened**: Error message, unexpected behavior
3. **Run ID**: If the issue involves a validation run
4. **Workflow ID or slug**: If the issue involves a specific workflow
5. **Timestamp**: When the issue occurred
6. **Screenshots**: If the issue is visual or UI-related

For self-hosted installations, also include:

- Validibot version
- Deployment environment (GCP, self-hosted Docker, etc.)
- Relevant log entries

### Support Channels

**Community Edition**: Open an issue on [GitHub](https://github.com/validibot/validibot/issues)

**Pro Edition**: Contact support via email (provided with your license)
