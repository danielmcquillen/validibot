# Frequently Asked Questions

Quick answers to common questions. For detailed information, follow the links to the full documentation.

## Access and Accounts

**How do I join an organization?**

You need an invitation from someone with Admin or Owner access. They'll send you an email invitation with a link to join. If you don't have a Validibot account yet, you'll create one during the signup process. See [Collaboration and Access](collaboration.md) for details.

**Can I belong to multiple organizations?**

Yes. Many users belong to several organizations. Use the organization switcher in the header to move between them. Each organization has its own workflows, projects, and member list.

**How do I create a new organization?**

From the organization switcher, click "Create Organization." You'll become the Owner of the new organization and can then invite others.

**What's the difference between Owner and Admin?**

Both can manage members and workflows, but only Owners can delete the organization or transfer ownership. Organizations can have multiple Owners and Admins. See the [role comparison table](collaboration.md#role-overview) for full details.

**Does Validibot support SSO?**

Enterprise deployments can integrate with LDAP or SAML identity providers. Contact your administrator or see the Enterprise edition documentation for setup instructions.

## Workflows and Validation

**What file types does Validibot support?**

Common formats include JSON, XML, YAML, CSV, and plain text (including EnergyPlus IDF files). The specific formats available depend on what validators are installed and what each workflow is configured to accept. See [Running Validations](running-validations.md) for details.

**Why can't I launch my workflow?**

The most common reasons:

1. **Workflow is inactive**: An Author or Admin needs to activate it
2. **No steps configured**: The workflow needs at least one validation step
3. **Insufficient permissions**: You need Executor role or higher

**How do I add custom validation rules?**

Workflow Authors can add CEL-based assertions to individual steps. For completely custom logic, you can create a custom validator. See [Workflow Management](workflow-management.md#step-assertions) for details on step assertions.

**What's the difference between a workflow being inactive and archived?**

- **Inactive**: The workflow exists and is visible, but won't accept new runs. Use this while editing or temporarily disabling a workflow.
- **Archived**: The workflow is hidden from the default list, won't accept runs, but all historical data is preserved. Use this for workflows you no longer need but want to keep for audit purposes.

**Why is my workflow paused?**

Workflows don't automatically pause. If a workflow is inactive, someone with Author or Admin access changed its status. Check the workflow's edit history or ask your team who made the change.

## Running Validations

**What's the maximum file size I can upload?**

This depends on your deployment configuration. The default limit is typically 10-50 MB, but administrators can adjust this. For very large files, consider using the API with streaming uploads or breaking files into smaller pieces.

**Why did my validation fail?**

Check the run's findings section. ERROR-level findings indicate what didn't pass. The message and path fields tell you what was wrong and where. See [Reviewing Results](reviewing-results.md) for guidance on interpreting findings.

**Can I retry a failed validation?**

Yes. From the run detail page, click "Rerun" to validate the same submission again. This is useful after you've fixed workflow rules or want to confirm the data hasn't changed.

**How long do validations take?**

Simple validations (schema checks) typically complete in seconds. Advanced validators like EnergyPlus or FMI simulation can take longer depending on the complexity of your data. The UI shows live progress while runs execute.

**What happens if I submit the wrong file?**

If the run is still in progress, you can cancel it from the run detail page. Otherwise, let it complete—the results will show that validation failed, but there's no harm done. You can then run a new validation with the correct file.

## API Integration

**Where do I find my API token?**

Log into Validibot, click your avatar, and go to Settings or Profile. Look for "API Tokens" or "Access Tokens." You can create new tokens there and copy them for use in your applications.

**Where do I find a workflow's ID or slug?**

- **In the UI**: Open the workflow and look at the URL—the slug is the last part (e.g., `/workflows/my-workflow-slug`)
- **In the API**: Call `GET /api/v1/orgs/{org_slug}/workflows/` to list all workflows with their IDs and slugs

**Should I use the workflow ID or slug in API calls?**

Slugs are recommended because they're human-readable and stable. IDs work too, but they're less meaningful. Both are accepted wherever `workflow_identifier` is required.

**How do I monitor long-running validations?**

Two options:

1. **Polling**: Submit the run, receive a 202 Accepted response, then poll the run detail endpoint until status is terminal
2. **Webhooks**: Configure a webhook endpoint in your workflow to receive a notification when runs complete (Pro edition)

**What's the API rate limit?**

Default limits vary by deployment. If you receive 429 (Too Many Requests) responses, implement exponential backoff. Contact your administrator if you need higher limits.

## Data and Privacy

**How long is validation data retained?**

This depends on your deployment's data retention policy. By default, runs and submissions are kept indefinitely. Administrators can configure automatic cleanup of old data.

**Can I delete a submission?**

Currently, submissions are retained for audit purposes. If you have compliance requirements around data deletion, contact your administrator about the retention policy.

**Is my data encrypted?**

Data at rest and in transit should be encrypted per your deployment's security configuration. Self-hosted deployments control their own encryption settings. See your administrator or the deployment documentation for specifics.

**Who can see my validation results?**

Only members of your organization can see detailed results. Access is controlled by roles—Viewers can see results, Executors can run validations, etc. See [Collaboration](collaboration.md) for the full permission model.

## Editions and Licensing

**What's the difference between Community and Pro editions?**

Community is free, open-source, and includes all core validation features. Pro adds CI/CD integration, machine-readable output formats (JUnit XML, SARIF), parallel execution, and commercial support. See [Editions](editions.md) for the full comparison.

**Can I use Community edition in CI/CD?**

The Community edition is designed for human-interactive use. It detects CI environments and will exit with an error. For CI/CD integration, you need the Pro edition.

**How do I upgrade to Pro?**

Visit [validibot.com/pricing](https://validibot.com/pricing) to purchase a Pro license. After purchase, you'll receive credentials to install the Pro package.

**What happens when my Pro license expires?**

You can continue using the installed version, but you won't be able to download updates or reinstall. Renew your license to restore full access.

## Getting More Help

**Where can I report a bug?**

For Community edition: [GitHub Issues](https://github.com/validibot/validibot/issues)

For Pro edition: Email support (included with your license)

**Is there a community forum or chat?**

Check the project's GitHub Discussions for community Q&A.

**Where can I request a new feature?**

Open an issue on GitHub describing your use case and the feature you'd like. Feature requests from Pro customers receive priority consideration.
