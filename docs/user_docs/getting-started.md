# Getting Started

This guide walks you through creating your first workflow and running a validation. By the end, you'll understand the basic Validibot workflow and be ready to build more sophisticated validation pipelines.

## Prerequisites

Before you begin, make sure you have:

- A Validibot account (either self-hosted or on the hosted service)
- Access to an organization (you may need an invitation from an admin)
- A sample file to validate (JSON, XML, or another supported format)

## Step 1: Sign In and Select Your Organization

When you sign in, you'll land on your default organization's dashboard. If you belong to multiple organizations, use the organization switcher in the header to select the one you want to work in.

Each organization is a separate workspace with its own workflows, projects, and team members. Your role within the organization determines what you can do:

- **Owner/Admin**: Full access to create, edit, and delete workflows; manage team members
- **Author**: Create and edit workflows
- **Executor**: Run workflows and view results
- **Viewer**: View workflows and results (read-only)

## Step 2: Create a Project

Projects help you organize related workflows. For example, you might have a project for "Building Models" and another for "Equipment Data."

1. Navigate to **Projects** in the sidebar.
2. Click **New Project**.
3. Enter a name and optional description.
4. Click **Create**.

If you're just getting started, you can use the default project that comes with your organization.

## Step 3: Create Your First Workflow

A workflow defines the validation steps your data will go through. Let's create a simple one:

1. Navigate to **Workflows** in the sidebar.
2. Click **New Workflow**.
3. Fill in the basics:
   - **Name**: Give it a descriptive name like "Product JSON Validation"
   - **Project**: Select the project this workflow belongs to
   - **Description**: Optional, but helpful for teammates
   - **Allowed File Types**: Select the formats this workflow accepts (JSON, XML, etc.)
4. Click **Create** to save the workflow.

## Step 4: Add a Validation Step

Your workflow needs at least one step to do anything useful. Let's add a validator:

1. From the workflow detail page, click **Add Step**.
2. Select a validator type. Common choices include:
   - **JSON Schema**: Validates JSON documents against a JSON Schema
   - **XML Schema**: Validates XML documents against an XSD
   - **Basic Validator**: Applies CEL-based assertions to JSON data
   - **AI Validator**: Uses AI to check data against natural-language rules
3. Configure the step:
   - **Name**: A short label like "Schema Check"
   - **Validator Settings**: Depends on the validator type (e.g., upload a schema file for JSON Schema validation)
4. Click **Save** to add the step.

You can add multiple steps to a workflow. They execute in order, and you can drag them to reorder.

## Step 5: Activate the Workflow

New workflows start in an inactive state. Before you can run validations:

1. Go to the workflow detail page.
2. Toggle the **Status** to **Active**.

Inactive workflows won't accept new validation runs. This lets you prepare and test workflows before making them available.

## Step 6: Run Your First Validation

Now let's see the workflow in action:

1. From the workflow detail page, click **Launch**.
2. The launch dialog appears. Either:
   - **Upload a file**: Click the upload area and select your test file.
   - **Paste content**: If your data is small, you can paste it directly.
3. Click **Run** to start the validation.

Validibot processes your submission through each workflow step. Depending on complexity, this may complete in seconds or take longer for advanced validators.

## Step 7: Review the Results

Once the run completes, you'll see the results page:

- **Run Status**: Shows whether the validation passed, failed, or encountered an error.
- **Step Results**: Each step shows its individual outcome and any findings.
- **Findings**: Specific issues discovered during validation, with severity levels (error, warning, info).

Click on any step to expand its details and see the specific assertions that passed or failed.

## What's Next?

Now that you've completed your first validation, you can:

- **[Manage Workflows](workflow-management.md)**: Learn how to edit, clone, and organize workflows.
- **[Explore Validators](../help_pages/validators/validators-overview.md)**: Understand the different validator types and their capabilities.
- **[Use the API](api-overview.md)**: Integrate Validibot into your CI/CD pipeline or other systems.
- **[Invite Teammates](collaboration.md)**: Share your workspace with colleagues.

## Tips for New Users

- **Start simple**: Create a workflow with one step, verify it works, then add complexity.
- **Use descriptive names**: "Q4 Compliance Check v2" is more helpful than "Test Workflow."
- **Check the allowed file types**: Make sure your workflow accepts the format you're submitting.
- **Review default assertions**: Many validators come with built-in checks. Look at "View Rules" to see what runs automatically.
