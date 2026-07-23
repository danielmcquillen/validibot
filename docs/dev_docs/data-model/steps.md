# Validation Step Runs

A **Validation Step Run** is the execution of one workflow step.  
Each step run belongs to exactly one Validation Run.

It records:

- Which validator/ruleset was applied.
- For action-based steps (integrations or certifications), which `Action` definition
  supplied the behaviour and the JSON configuration used for that run.
- Status (`PENDING`, `RUNNING`, `PASSED`, `FAILED`, `SKIPPED`).
- Timestamps and duration.
- Machine-readable output and error messages.
- Links to any **findings** produced during the step.

## Step Definitions (WorkflowStep)

`WorkflowStep` rows (`validibot/workflows/models.py`) describe the
authored workflow. Key relationships:

- Each step belongs to a workflow and has an `order` field that determines the
  linear execution sequence.
- A step must reference **either** a `validator` **or** an `action` (enforced via
  a database check constraint). Validator steps run a validator and produce findings.
  Action steps trigger side effects such as Slack notifications or issuing a
  signed credential; they do not execute assertions.
- `ruleset` is **optional** and only meaningful for validator steps. When present
  it overrides the validator’s default ruleset so this step can enforce stricter
  or looser assertions. When `ruleset` is blank, the validator’s
  `default_ruleset` (if any) is used.
- `config` and `display_settings` are two JSON columns that together hold a
  step's settings, split by whether they change what validation *does*
  (ADR-2026-06-18):
    - **`config`** is the *semantic* bucket — only settings that affect the
      validation result (`schema_type`, `delimiter`, `encoding`, `has_header`,
      `case_sensitive`, FMU simulation settings, the container
      `execution_profile`, and so on). Its per-validator
      Pydantic models (`workflows/step_configs.py`) use `extra="forbid"`, so
      nothing cosmetic or run-injected can land here. Because of that guarantee,
      the workflow-definition digest (`services/contract_snapshot.py`) hashes
      this whole field — a change to any key re-bases the hash, which is exactly
      what you want for a value that changes pass/fail.
    - **`display_settings`** is the *cosmetic + runtime-injected* bucket —
      human labels, text previews, column counts, `display_step_outputs`, and the
      keys the runner injects at launch time (`primary_file_uri`, …). Its models
      use `extra="allow"`, and it is **never** hashed, so editing a label or
      preview can't invalidate a prior attestation.
  Use `step.typed_config` and `step.display_settings_typed` for type-safe access
  to each bucket. New keys should go in whichever bucket matches this rule: does
  it change the validation result? If yes, `config`; if it's only for display or
  is injected at run time, `display_settings`. Both buckets round-trip through
  workflow import/export (VAF).
- Container-based steps accept `execution_profile=FAST_RESPONSE` (the stable
  default, omitted from canonical stored JSON) or `LONG_RUNNING`. This is
  author intent, not a provider name. Managed GCP resolves the former through
  the active primary route and the latter through the retained long-running
  route before it contacts a provider. The chosen profile and exact deployment
  are then captured on the execution attempt.
- `display_schema` is limited to validator steps; action steps automatically
  disable it.

## Rulesets, Validators, and Actions

- **Validators** (`validibot/validations/models.py`) encapsulate a
  validator class plus its catalog. They may declare a `default_ruleset` that
  ships with baseline assertions.
- **Rulesets** capture the assertions that workflow authors want to execute for a
  given validator. They are versioned separately so the same validator can power
  many workflows with different policies. Rulesets must match the validator’s
  `validation_type` (enforced in `WorkflowStep.clean()`), which keeps JSON steps
  from referencing XML rules, etc.
- **Actions** (`validibot/actions/models.py` subclasses under
  `Action`) represent non-validation steps. They persist their own structured
  configuration (Slack message body and similar action-specific data) and are linked
  from `WorkflowStep.action`.

During execution the runtime inspects the step:

1. Validator step → load the validator, merge any per-step `config`, resolve the
   attached ruleset (falling back to the validator default), and run the
   validator.
2. Action step → instantiate the concrete action subclass with the stored data
   and invoke its handler. No ruleset is involved.

This separation keeps validation concerns declarative while leaving side effects
isolated in the action subsystem.

## Action Variants

Action-based workflow steps now persist their configuration on concrete subclasses of
`Action`. The initial set includes `SlackMessageAction` (stores the Slack message body)
and `SignedCredentialAction` (which currently adds no extra authored fields beyond the
base action metadata). Each variant ships with a dedicated form so authors configure
only the fields that matter instead of hand-editing JSON blobs. Workflow steps keep a
short summary of the action data in `WorkflowStep.config` so the UI can render a
preview without resolving every related object.
