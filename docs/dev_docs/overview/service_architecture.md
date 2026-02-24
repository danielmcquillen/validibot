# Service Layer Architecture

This document describes how the validation run service layer is decomposed into
focused modules, the rationale behind the structure, and how to extend it.

## Why We Split the Service

`ValidationRunService` started as a single class handling everything: run
lifecycle, step iteration, finding persistence, summary aggregation, step
dispatch, and cross-step signal extraction. At over 1200 lines it was becoming
difficult to reason about, test in isolation, and modify safely.

We decomposed it into four modules, each with a single responsibility:

```
ValidationRunService (facade)
├── launch()                        — create run, dispatch to worker
├── cancel_run()                    — cancel a pending/running run
├── execute_workflow_steps()        → StepOrchestrator
└── rebuild_run_summary_record()    → SummaryBuilder

StepOrchestrator
├── execute_workflow_steps()        — iterate steps sequentially
├── execute_workflow_step()         — dispatch single step to handler
├── _start_step_run()               — idempotent step creation
├── _finalize_step_run()            — persist status, duration, output
├── _record_step_result()           — persist findings (action steps only)
├── _execute_validator_step()       — call step processor
├── _extract_downstream_signals()   — collect cross-step signals
└── _resolve_run_actor()            — resolve the initiating user

SummaryBuilder
├── rebuild_run_summary_record()    — idempotent public entry point
├── build_run_summary_record()      — aggregate from DB findings
└── extract_assertion_total()       — extract assertion count from stats

FindingsPersistence
├── normalize_issue()               — coerce raw issues to ValidationIssue
├── coerce_severity()               — map arbitrary severity to enum
├── severity_value()                — resolve string for DB storage
└── persist_findings()              — bulk-create ValidationFinding rows
```

## Design Principles

### Facade pattern preserves the public API

`ValidationRunService` remains the single import point for all callers. Views,
API endpoints, task queues, and callback handlers all continue to import from
`validations.services.validation_run`. The facade delegates to internal modules,
so no call sites need updating.

### Pure functions where possible

`FindingsPersistence` and `SummaryBuilder` expose standalone functions rather
than classes. They have no state, no side effects beyond database writes, and
are easy to test in isolation. The `StepOrchestrator` is a class because its
methods call each other and share the same execution context.

### Incremental extraction

Each module was extracted as a standalone step and tested before proceeding.
This minimises risk when refactoring a critical code path.

## Module Responsibilities

### ValidationRunService (`validation_run.py`)

The public facade. Owns two web-layer operations:

- **`launch()`** — validates preconditions (permissions, limits), creates the
  `ValidationRun` record, and dispatches execution to the appropriate backend
  (Celery, Cloud Tasks, inline for tests).
- **`cancel_run()`** — transitions a pending/running run to CANCELED.

Everything else is delegated.

### StepOrchestrator (`step_orchestrator.py`)

The worker-side execution orchestrator. Handles:

- **State transitions**: PENDING to RUNNING (atomic, idempotent).
- **Step iteration**: Processes steps sequentially, stopping on failure or
  when an async validator returns pending.
- **Step dispatch**: Routes validator steps to the processor pattern and
  action steps to the handler registry.
- **Step lifecycle**: Creates step runs (idempotent via `get_or_create`),
  finalises them with status, duration, and diagnostics.
- **Result recording**: Persists findings, extracts signals for downstream
  assertions.
- **Run finalisation**: Sets terminal status, builds summary, logs tracking
  events, triggers submission purge.

### SummaryBuilder (`summary_builder.py`)

Aggregates run-level and step-level summary records from persisted findings.
Queries the database rather than relying on in-memory metrics, making it safe
to call in resume scenarios (async callbacks, retries).

### FindingsPersistence (`findings_persistence.py`)

Handles the conversion of raw validation issues into normalised
`ValidationFinding` database records. Pure functions with no dependencies on
other service methods.

## Type-Safe Step Config

Each validator and action type stores different keys in `WorkflowStep.config`
(a JSONField). The `workflows/step_configs.py` module provides Pydantic models
that give these configs type safety:

```python
from validibot.workflows.step_configs import get_step_config

typed = get_step_config(step)
if isinstance(typed, EnergyPlusStepConfig):
    file_ids = typed.resource_file_ids  # list[str], type-checked
```

All models use `extra="allow"` so runtime-injected keys (like
`primary_file_uri` added during container launch) don't cause validation
errors.

The `WorkflowStep.typed_config` property provides convenient access:

```python
config = step.typed_config  # Returns the appropriate Pydantic model
```

Config validation also runs during `WorkflowStep.clean()`, catching type
mismatches at save time rather than at runtime.

### Available Config Models

| Validator / Action | Model | Key Fields |
|--------------------|-------|------------|
| JSON_SCHEMA | `JsonSchemaStepConfig` | `schema_source`, `schema_type`, `schema_text_preview` |
| XML_SCHEMA | `XmlSchemaStepConfig` | `schema_source`, `schema_type`, `schema_text_preview` |
| ENERGYPLUS | `EnergyPlusStepConfig` | `resource_file_ids`, `idf_checks`, `run_simulation`, `timestep_per_hour` |
| AI_ASSIST | `AiAssistStepConfig` | `template`, `mode`, `cost_cap_cents`, `selectors`, `policy_rules` |
| BASIC | `BasicStepConfig` | (empty) |
| FMI | `FmiStepConfig` | (empty) |
| CUSTOM_VALIDATOR | `CustomValidatorStepConfig` | (empty) |
| SLACK_MESSAGE | `SlackActionStepConfig` | `message` |
| SIGNED_CERTIFICATE | `CertificateActionStepConfig` | `certificate_template` |

### Adding a New Validator Type

1. Add the type to `ValidationType` in `validations/constants.py`.
2. Create a Pydantic model in `workflows/step_configs.py` extending
   `BaseStepConfig`.
3. Register it in the `STEP_CONFIG_MODELS` dict.
4. Create the validator class implementing `BaseValidator`.
5. Register the validator with `@register_validator`.

## File Structure

```
validations/services/
├── validation_run.py          # Public facade (launch, cancel, delegation)
├── step_orchestrator.py       # Worker-side step execution loop
├── summary_builder.py         # Run/step summary aggregation
├── findings_persistence.py    # Issue normalization and finding creation
├── validation_callback.py     # Async validator callback handling
├── models.py                  # ValidationRunTaskResult dataclass
└── step_processor/            # Step processor pattern (see step_processor.md)
    ├── base.py
    ├── simple.py
    ├── advanced.py
    ├── factory.py
    └── result.py

workflows/
├── models.py                  # WorkflowStep.typed_config property
└── step_configs.py            # Pydantic models for step config
```

## Related Documentation

- [How Validibot Works](how_it_works.md) — end-to-end system overview
- [Step Processor Architecture](step_processor.md) — processor pattern details
- [Workflow Engine](workflow_engine.md) — higher-level orchestration
- [Validator Architecture](validator_architecture.md) — execution backends
