# Validation Step Runs

A **Validation Step Run** is the execution of one workflow step.  
Each step run belongs to exactly one Validation Run.

It records:

- Which validator/ruleset was applied.
- For action-based steps (integrations or certifications), which `Action` definition
  supplied the behaviour and the JSON configuration used for that run.
- Status (`pending`, `running`, `passed`, `failed`, `skipped`).
- Timestamps and duration.
- Machine-readable output and error messages.
- Links to any **findings** produced during the step.

## Action Variants

Action-based workflow steps now persist their configuration on concrete subclasses of
`Action`. The initial set includes `SlackMessageAction` (stores the Slack message body)
and `SignedCertificateAction` (stores the uploaded certificate template, or uses the
bundled `default_signed_certificate.pdf` when no upload is provided). Each variant
ships with a dedicated form so authors configure only the fields that matter—no more
hand-editing JSON blobs. Workflow steps keep a short summary of the action data in
`WorkflowStep.config` so the UI can render a preview without resolving every file.
