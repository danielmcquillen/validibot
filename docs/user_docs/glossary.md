# Glossary

Quick definitions of Validibot terminology. For detailed explanations, follow the links to the relevant documentation.

## A

**Action Step**
A workflow step that performs an action other than validation, such as sending a notification, generating a certificate, or triggering an external system. Compare with *Validator Step*.

**Admin**
A role that can manage organization members, invite new users, and edit all workflows. Admins cannot delete organizations or manage billing. See [Collaboration](collaboration.md).

**Assertion**
A rule that must be true for validation to pass. Assertions can be built into validators (default assertions) or added to individual workflow steps (step assertions). Assertions often use CEL expressions.

**Author**
A role that can create and edit workflows, manage validators, and run validations. Authors can only edit workflows they created, unlike Admins who can edit any workflow. See [Collaboration](collaboration.md).

## C

**CEL (Common Expression Language)**
A simple expression language used to write validation rules. CEL expressions evaluate to true or false and can reference fields in your data.

## E

**Executor**
A role that can run validations on active workflows and view results. Executors cannot create or modify workflows. This role is typical for team members who submit data but don't configure validation logic. See [Collaboration](collaboration.md).

## F

**Finding**
A single issue discovered during validation. Each finding has a severity (ERROR, WARNING, INFO), a message, and often a path indicating where in the data the issue was found. Findings are the primary output of validation runs.

## O

**Organization**
The top-level workspace in Validibot. Each organization has its own members, workflows, projects, and settings. Users can belong to multiple organizations and switch between them using the organization selector.

**Owner**
The highest-privilege role in an organization. Owners have full control including the ability to delete the organization and manage billing (Pro edition). Every organization must have at least one Owner. See [Collaboration](collaboration.md).

## P

**Project**
A way to group related workflows within an organization. Projects help organize your validation library by topic, team, or data type. All organization members can access all projects based on their role.

## R

**Result**
The overall outcome of a validation run: PASS, FAIL, ERROR, CANCELED, or TIMED_OUT. The result is derived from the run's status and findings. See [Reviewing Results](reviewing-results.md).

**Role**
A permission level assigned to organization members. Validibot has five roles: Owner, Admin, Author, Executor, and Viewer. Higher roles include all permissions of lower roles. See [Collaboration](collaboration.md).

**Run**
See *Validation Run*.

## S

**Severity**
The importance level of a finding: ERROR (blocks validation from passing), WARNING (informational but doesn't block), or INFO (purely informational). See [Reviewing Results](reviewing-results.md).

**Slug**
A URL-friendly identifier for workflows and organizations. Slugs are generated from names and are unique within their scope. For example, "Product Schema Validation" might have the slug `product-schema-validation`.

**Status**
The current state of a validation run: PENDING, RUNNING, SUCCEEDED, FAILED, CANCELED, or TIMED_OUT. Status reflects where the run is in its lifecycle.

**Step**
See *Workflow Step*.

**Submission**
The data payload submitted for validation. A submission can be a file upload, pasted content, or data sent via API. Submissions are stored and associated with validation runs for audit and rerun purposes.

## V

**Validation Run**
A single execution of a workflow against a submission. Each run processes the submission through the workflow's steps and produces findings. Runs are immutable records that can be reviewed, shared, and rerun.

**Validator**
An engine that performs a specific type of validation. Built-in validators include JSON Schema, XML Schema, Basic (CEL assertions), and AI. Advanced validators include EnergyPlus and FMI simulation. Validators can be system-provided or custom.

**Validator Step**
A workflow step that runs a validator against the submission to check for issues. Most workflow steps are validator steps. Compare with *Action Step*.

**Viewer**
A read-only role that can view workflows and validation results but cannot make changes or run validations. Useful for stakeholders and auditors. See [Collaboration](collaboration.md).

## W

**Webhook**
An HTTP callback that notifies external systems when validation events occur. Webhooks can trigger when runs complete, allowing integration with CI/CD systems, notification platforms, and other tools.

**Workflow**
An ordered sequence of validation steps owned by an organization and assigned to a project. When you submit data for validation, it runs through each step in order. Workflows can be active (accepting runs), inactive (visible but not accepting runs), or archived (hidden but preserved for history).

**Workflow Step**
A single action within a workflow. Steps execute in order and are typically either validator steps (checking data) or action steps (performing other tasks like notifications). Each step produces its own findings.
