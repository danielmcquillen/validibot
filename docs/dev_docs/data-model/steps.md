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

## Step Definitions (WorkflowStep)

`WorkflowStep` rows (`validibot/workflows/models.py:490-575`) describe the
authored workflow. Key relationships:

- Each step belongs to a workflow and has an `order` field that determines the
  linear execution sequence.
- A step must reference **either** a `validator` **or** an `action` (enforced via
  a database check constraint). Validator steps run an engine and produce findings.
  Action steps trigger side effects such as Slack notifications or issuing a
  certificate; they do not execute assertions.
- `ruleset` is **optional** and only meaningful for validator steps. When present
  it overrides the validator’s default ruleset so this step can enforce stricter
  or looser assertions. When `ruleset` is blank, the validator’s
  `default_ruleset` (if any) is used.
- `config` is a JSON column for per-step overrides (severity thresholds, AI
  templates, etc.) passed straight into the validator engine or action class.
- `display_schema` is limited to validator steps; action steps automatically
  disable it.

## Rulesets, Validators, and Actions

- **Validators** (`validibot/validations/models.py:562-760`) encapsulate a
  validation engine plus its catalog. They may declare a `default_ruleset` that
  ships with baseline assertions.
- **Rulesets** capture the assertions that workflow authors want to execute for a
  given validator. They are versioned separately so the same validator can power
  many workflows with different policies. Rulesets must match the validator’s
  `validation_type` (enforced in `WorkflowStep.clean()`), which keeps JSON steps
  from referencing XML rules, etc.
- **Actions** (`validibot/validations/models.py` subclasses under
  `Action`) represent non-validation steps. They persist their own structured
  configuration (Slack message body, certificate template, etc.) and are linked
  from `WorkflowStep.action`.

During execution the runtime inspects the step:

1. Validator step → load the validator, merge any per-step `config`, resolve the
   attached ruleset (falling back to the validator default), and run the
   validator engine.
2. Action step → instantiate the concrete action subclass with the stored data
   and invoke its handler. No ruleset is involved.

This separation keeps validation concerns declarative while leaving side effects
isolated in the action subsystem.

## Action Variants

Action-based workflow steps now persist their configuration on concrete subclasses of
`Action`. The initial set includes `SlackMessageAction` (stores the Slack message body)
and `SignedCertificateAction` (stores the uploaded certificate template, or uses the
bundled `default_signed_certificate.pdf` when no upload is provided). Each variant
ships with a dedicated form so authors configure only the fields that matter—no more
hand-editing JSON blobs. Workflow steps keep a short summary of the action data in
`WorkflowStep.config` so the UI can render a preview without resolving every file.
