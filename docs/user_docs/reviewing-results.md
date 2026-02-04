# Reviewing Results

After a validation run completes, you'll want to understand what passed, what failed, and why. This guide explains how to interpret findings, navigate the results interface, and share outcomes with your team.

## The Run Summary

When you open a completed run, you'll see the summary panel at the top:

**Status badge**: Shows the overall outcome (SUCCEEDED, FAILED, CANCELED, etc.)

**Result**: The automation-friendly conclusion:

- **PASS** — No errors found; the data meets all requirements
- **FAIL** — Errors were found; the data didn't pass validation
- **ERROR** — Something went wrong during validation (not a data problem)

**Timestamps**: When the run started and ended, plus total duration

**Workflow link**: Click to see the workflow definition that was used

**Submission info**: The filename, size, and content type of what was validated

## Step-by-Step Results

Below the summary, you'll see each workflow step with its individual outcome. Steps are shown in execution order.

### Step Status Icons

- ✓ **Passed** — This step completed without errors
- ✗ **Failed** — This step found errors or encountered a problem
- ⏸ **Skipped** — This step didn't run (usually because a previous step failed)
- ⏳ **Running** — This step is currently executing

### Expanding Step Details

Click on any step to expand its details:

**Validator info**: Which validator ran and what it checked

**Findings**: The specific issues found by this step

**Assertions evaluated**: How many rules were checked and how many passed

**Duration**: How long this step took to execute

**Output**: For advanced validators (like FMI or EnergyPlus), you may see additional output data or signals extracted from the validation process

## Understanding Findings

Findings are the core output of validation. Each finding represents something the validator noticed about your data.

### Severity Levels

| Severity | Meaning | Effect on Result |
|----------|---------|------------------|
| **ERROR** | A problem that must be fixed | Causes the step (and run) to fail |
| **WARNING** | Something worth reviewing but not blocking | Run can still pass |
| **INFO** | Informational note | No effect on pass/fail |

### Finding Details

Each finding includes:

**Message**: A human-readable description of what was found. For example: "Required property 'name' is missing" or "Value exceeds maximum threshold of 100."

**Path**: For structured data (JSON, XML), this shows where in the document the issue was found. For example: `data.users[0].email` or `/building/zone[2]/name`.

**Code**: A machine-readable identifier for the issue type. Useful for automation and filtering.

**Source**: Which assertion or rule generated this finding.

### Reading JSON Paths

When reviewing findings for JSON data, paths use dot notation:

- `root.field` — A field called "field" inside the root object
- `items[0]` — The first element of an array called "items"
- `users[2].address.city` — The city field, inside the address of the third user

### Reading XML Paths

For XML documents, paths use XPath-style notation:

- `/root/element` — An element inside the root
- `/root/items/item[1]` — The first item element
- `/root/element/@attribute` — An attribute on an element

## Filtering and Searching Findings

For runs with many findings, use the filtering options:

**By severity**: Show only errors, only warnings, or all findings

**By step**: Focus on findings from a specific workflow step

**Search**: Type keywords to find specific findings by message content

## Assertion Statistics

At the bottom of each step (and summarized for the run), you'll see assertion statistics:

- **Total assertions**: How many rules were evaluated
- **Passed**: Rules that succeeded
- **Failed**: Rules that generated error findings
- **Warnings**: Rules that generated warning findings

These statistics help you understand coverage—how thoroughly your data was checked.

## Sharing Results

### Copying Run Links

To share a run with teammates:

1. From the run detail page, copy the URL from your browser
2. Share it with anyone who has access to the organization

Recipients need at least Viewer access to see the run details.

### Exporting Results

Depending on your Validibot edition, you may be able to export results:

**JSON export**: Machine-readable format for integration with other tools

**PDF report**: Formatted document for stakeholders (Pro edition)

**JUnit XML**: CI/CD compatible format for test dashboards (Pro edition)

### Public vs Private Information

Some workflows include a "public information" description that's visible to anyone. The actual findings and detailed results are only visible to organization members with appropriate access.

## Comparing Runs

To see how validation results change over time:

1. Navigate to the workflow detail page
2. View the list of recent runs
3. Compare status and finding counts across runs

This helps identify whether data quality is improving or if new issues have been introduced.

## What to Do When Validation Fails

When a run fails, here's a systematic approach:

1. **Check the error findings**: Start with ERROR-level findings since they caused the failure

2. **Identify the pattern**: Are all errors from one step? One type of issue? This suggests where to focus

3. **Review the data**: Use the path information to find the problematic section in your source data

4. **Fix and rerun**: Correct the issues in your data and run the validation again

5. **Check warnings too**: Once errors are fixed, review warnings for additional quality improvements

## Understanding Error Categories

Sometimes runs fail not because of data problems but due to system issues. The error category helps distinguish:

**Data errors**: Your data didn't meet the validation rules. Fix the data and rerun.

**Configuration errors**: Something's wrong with the workflow or validator setup. Contact your workflow author.

**System errors**: A technical problem occurred. Try again, or contact your administrator if it persists.

**Timeout errors**: The validation took too long. This might indicate overly complex data or resource constraints.
