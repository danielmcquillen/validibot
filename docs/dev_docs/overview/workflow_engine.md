# Workflow Engine Architecture

This document describes the internal architecture of the `ValidationRunService`, which orchestrates the execution of validation workflows.

## Overview

The **Workflow Engine** is responsible for:
1. Iterating through `WorkflowStep`s defined in a `Workflow`
2. Executing each step against a `Submission`
3. Recording the results (`ValidationFinding`, status updates)
4. Handling workflow-level status transitions

## Architecture: Two-Layer Dispatch

The workflow engine uses a two-layer dispatch pattern:

1. **ValidationRunService** - Orchestrates the workflow loop and delegates individual steps
2. **Processors/Handlers** - Execute individual steps based on their type

```
ValidationRunService.execute_workflow_steps()
        │
        ├── For validator steps:
        │       │
        │       └── ValidationStepProcessor
        │               ├── SimpleValidationProcessor (JSON, XML, Basic, AI)
        │               └── AdvancedValidationProcessor (EnergyPlus, FMI)
        │
        └── For action steps:
                │
                └── StepHandler
                        ├── SlackMessageActionHandler
                        ├── SignedCertificateActionHandler
                        └── ...
```

## Validator Step Execution: The Processor Pattern

Validator steps are executed through the **ValidationStepProcessor** abstraction. This provides a clean separation between:

- **Workflow orchestration** (ValidationRunService) - loops, aggregation, status management
- **Step lifecycle** (Processors) - call engine, persist findings, handle errors
- **Validation logic** (Engines) - schema checking, AI prompts, assertions

### How Validator Steps Execute

```python
# Inside ValidationRunService.execute_workflow_steps()
for step in workflow_steps:
    step_run = self._start_step_run(validation_run, step)

    if step.validator:
        # Use processors for validator steps
        metrics = self._execute_validator_step(
            validation_run=validation_run,
            step_run=step_run,
        )
    else:
        # Use existing handler flow for action steps
        validation_result = self.execute_workflow_step(step=step, ...)
```

The `_execute_validator_step()` method delegates to the appropriate processor:

```python
def _execute_validator_step(self, validation_run, step_run):
    from validibot.validations.services.step_processor import get_step_processor

    processor = get_step_processor(validation_run, step_run)
    result = processor.execute()

    return {
        "step_run": result.step_run,
        "severity_counts": result.severity_counts,
        "total_findings": result.total_findings,
        "assertion_failures": result.assertion_failures,
        "assertion_total": result.assertion_total,
        "passed": result.passed,
    }
```

### Processor Types

| Processor | Validator Types | Execution Mode |
|-----------|-----------------|----------------|
| `SimpleValidationProcessor` | Basic, JSON Schema, XML Schema, AI | Synchronous, inline |
| `AdvancedValidationProcessor` | EnergyPlus, FMI, custom | Sync (Docker) or Async (Cloud Run) |

For detailed documentation on processors, see [Validation Step Processor Architecture](step_processor.md).

## Action Step Execution: The Handler Pattern

Action steps (non-validation operations) use the **StepHandler** protocol for extensibility.

### Core Components

#### 1. Protocol (`StepHandler`)
All execution logic must implement the `StepHandler` protocol defined in `validibot/actions/protocols.py`:

```python
class StepHandler(Protocol):
    def execute(self, run_context: RunContext) -> StepResult:
        ...
```

- **RunContext**: Contains the `ValidationRun`, `WorkflowStep`, and shared signals.
- **StepResult**: Standardized output indicating pass/fail, issues, and statistics.

#### 2. Dispatcher (`ValidationRunService`)
For action steps, the service:
- Resolves the appropriate implementation (`Action` subclass)
- Looks up the registered `StepHandler`
- Invokes `handler.execute(context)`

### Available Handlers

| Handler | Purpose |
|---------|---------|
| `SlackMessageActionHandler` | Sends Slack notifications |
| `SignedCertificateActionHandler` | Generates and attaches certificates |

## Async Validator Completion: Callbacks

When advanced validators run on async backends (like GCP Cloud Run), execution follows a two-phase pattern:

### Phase 1: Launch (ValidationRunService)
1. Processor calls `engine.validate()`, which launches container
2. Container job starts running on Cloud Run
3. Processor returns `StepProcessingResult(passed=None)`
4. Run stays in `RUNNING` status, waiting for callback

### Phase 2: Complete (ValidationCallbackService)
1. Container completes and POSTs callback to `/api/internal/callbacks/validation/`
2. `ValidationCallbackService` downloads output envelope from cloud storage
3. Creates processor and calls `processor.complete_from_callback(output_envelope)`
4. Processor finalizes step and either:
   - Resumes workflow with next step, OR
   - Finalizes run as SUCCEEDED/FAILED

```python
# Inside ValidationCallbackService._process_callback()
from validibot.validations.services.step_processor import get_step_processor

processor = get_step_processor(run, step_run)
processor.complete_from_callback(output_envelope)
```

## Extending the Engine

### Adding a New Validator Type

1. **Create the engine** in `validibot/validations/engines/`:
   - Extend `BaseValidatorEngine`
   - Implement `validate()` method
   - For container-based validators, implement `post_execute_validate()` too

2. **Register the engine** in `validibot/validations/engines/registry.py`:
   ```python
   register(ValidationType.MY_VALIDATOR, MyValidatorEngine)
   ```

3. **Update processor factory** (if needed) in `step_processor/factory.py`:
   ```python
   advanced_types = {
       ValidationType.ENERGYPLUS,
       ValidationType.FMI,
       ValidationType.MY_VALIDATOR,  # Add here if container-based
   }
   ```

### Adding a New Action Type

1. **Define the Model**: Create a new `Action` subclass in `validibot/actions/models.py`
2. **Implement Handler**: Create a class implementing `StepHandler` in `validibot/actions/handlers.py`
3. **Register**: Map the action type to your handler in `validibot/actions/registry.py`:

```python
# validibot/actions/registry.py
register_action_handler(MyActionType.CUSTOM, MyCustomHandler)
```

## Execution Flow Summary

```
┌────────────────────────────────────────────────────────────────────┐
│                     API Request Arrives                             │
└─────────────────────────────┬──────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────┐
│          ValidationRunService.execute_workflow_steps()              │
│                                                                     │
│   1. Mark run as RUNNING                                            │
│   2. Log VALIDATION_RUN_STARTED event                               │
│   3. For each workflow step:                                        │
│      a. Create/get ValidationStepRun                                │
│      b. Route to processor (validator) or handler (action)          │
│      c. Aggregate metrics                                           │
│   4. Build run summary                                              │
│   5. Finalize run status                                            │
│   6. Log VALIDATION_RUN_SUCCEEDED/FAILED event                      │
└────────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              │                               │
              ▼                               ▼
┌─────────────────────────┐     ┌─────────────────────────┐
│   Validator Step        │     │   Action Step           │
│                         │     │                         │
│   get_step_processor()  │     │   get_action_handler()  │
│   processor.execute()   │     │   handler.execute()     │
└─────────────────────────┘     └─────────────────────────┘
```

## Related Documentation

- [Validation Step Processor Architecture](step_processor.md) - Deep dive into processor pattern
- [Validator Architecture](validator_architecture.md) - Execution backends and deployment
- [How Validibot Works](how_it_works.md) - End-to-end system overview
- [ADR: Validation Step Processor Refactor](../adr/2026-01-30-validation-step-processor-refactor.md) - Design decision
