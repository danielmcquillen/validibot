# Validation Step Processor Architecture

This document provides a comprehensive guide to how Validibot executes validation steps, explaining the processor pattern, the different validator types, and how the system handles both synchronous and asynchronous execution.

## Overview

The **Validation Step Processor** is the core abstraction that orchestrates the execution of individual validation steps within a workflow. It sits between the step orchestrator (which iterates through workflow steps) and the low-level validation logic (validators), providing a clean separation of concerns.

```
┌─────────────────────────────────────────────────────────────────────┐
│               ValidationRunService (Facade)                         │
│            (Launch, Cancel, Delegation)                              │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    StepOrchestrator                                  │
│                 (Step Iteration & Dispatch)                          │
│                                                                      │
│   Responsibilities:                                                  │
│   - Loop through workflow steps                                      │
│   - Create ValidationStepRun records                                 │
│   - Route to processors (validators) or handlers (actions)           │
│   - Handle workflow-level status transitions                         │
│   - Delegate to SummaryBuilder and FindingsPersistence               │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  ValidationStepProcessor                             │
│                   (Step Lifecycle)                                   │
│                                                                      │
│   Responsibilities:                                                  │
│   - Call engine methods at the right time                            │
│   - Persist findings to database                                     │
│   - Store signals for downstream steps                               │
│   - Handle errors gracefully                                         │
│   - Finalize step with timing and status                             │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Validators                                    │
│                  (Validation Logic)                                  │
│                                                                      │
│   Responsibilities:                                                  │
│   - Execute validation logic (schema checking, AI prompts, etc.)     │
│   - Evaluate CEL assertions                                          │
│   - Extract signals/metrics from outputs                             │
│   - Return structured ValidationResult                               │
└─────────────────────────────────────────────────────────────────────┘

See [Service Layer Architecture](service_architecture.md) for the full
decomposition of the service layer.
```

## Two Types of Validators

Validibot distinguishes between two categories of validators based on how they execute:

### Simple Validators (Inline)

**Built-in validators**: Basic, JSON Schema, XML Schema, AI

These validators:
- Run directly in the Django process
- Complete synchronously (blocking)
- Have a single assertion stage (input-only)
- Are fast and lightweight

```python
# Simple validator flow
result = engine.validate(submission, ruleset, run_context)
# → Validation logic runs
# → Input-stage assertions evaluated
# → Returns complete result immediately
```

### Advanced Validators (Container-based)

**Container validators**: EnergyPlus, FMU, user-added custom validators

These validators:
- Run inside Docker containers
- May complete synchronously or asynchronously (depending on deployment)
- Have two assertion stages (input AND output)
- Can be computationally intensive

```python
# Advanced validator flow (sync)
result = engine.validate(...)           # Launches container, blocks until done
post_result = engine.post_execute_validate(output_envelope)  # Processes results

# Advanced validator flow (async)
result = engine.validate(...)           # Launches container, returns immediately
# ... later, callback arrives ...
post_result = engine.post_execute_validate(output_envelope)  # Processes results
```

## The Processor Pattern

### Why Processors?

Before the processor pattern, validation step logic was scattered across:
- `StepOrchestrator._record_step_result()` - for sync execution
- `ValidationCallbackService._process_callback()` - for async callbacks

This led to code duplication, inconsistent behavior, and difficult maintenance. The processor pattern consolidates validator step logic into a single, testable abstraction. `_record_step_result()` now only handles action steps (Slack, certificates, etc.).

### Processor Class Hierarchy

```
ValidationStepProcessor (abstract base)
├── SimpleValidationProcessor
│   └── Handles: Basic, JSON Schema, XML Schema, AI validators
└── AdvancedValidationProcessor
    └── Handles: EnergyPlus, FMU, custom container validators
```

### Processor Responsibilities

| Responsibility | Description |
|----------------|-------------|
| Validator dispatch | Call `engine.validate()` and `engine.post_execute_validate()` |
| Finding persistence | Save `ValidationFinding` records to database |
| Signal storage | Store extracted metrics for downstream steps |
| Assertion tracking | Record assertion counts for run summaries |
| Error handling | Catch exceptions and set appropriate status |
| Step finalization | Set ended_at, duration_ms, status, output |

### What Processors Do NOT Do

Processors handle lifecycle, not logic. They do NOT:
- Evaluate CEL assertions (validator's job)
- Extract signals from output data (validator's job)
- Know about validation semantics (validator's job)

## Detailed Execution Flows

### Flow 1: Simple Validator (JSON Schema)

This is the simplest case - a single method call that completes synchronously.

```
┌─────────────────┐
│ ValidationRun   │
│ Service         │
└────────┬────────┘
         │
         │ 1. Get processor for step
         ▼
┌─────────────────┐
│ SimpleValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 2. processor.execute()
         ▼
┌─────────────────┐
│ JsonSchema      │
│ Validator       │
└────────┬────────┘
         │
         │ 3. engine.validate()
         │    - Load schema from ruleset
         │    - Parse submission JSON
         │    - Run jsonschema validation
         │    - Evaluate input-stage CEL assertions
         │    - Return ValidationResult
         │
         ▼
┌─────────────────┐
│ SimpleValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 4. persist_findings(result.issues)
         │ 5. store_assertion_counts(...)
         │ 6. finalize_step(status, stats)
         │
         ▼
┌─────────────────┐
│ StepProcessing  │
│ Result          │
│ (passed=True)   │
└─────────────────┘
```

**Code path:**
```
validibot/validations/services/validation_run.py  (facade)
  └── execute_workflow_steps() → delegates to StepOrchestrator

validibot/validations/services/step_orchestrator.py
  └── execute_workflow_steps()
      └── _execute_validator_step()
          └── processor.execute()

validibot/validations/services/step_processor/simple.py
  └── SimpleValidationProcessor.execute()
      └── engine.validate()
      └── persist_findings()
      └── store_assertion_counts()
      └── finalize_step()
```

### Flow 2: Advanced Validator - Sync (Docker Compose Deployments)

When running with Docker Compose, container execution blocks until complete.

```
┌─────────────────┐
│ ValidationRun   │
│ Service         │
└────────┬────────┘
         │
         │ 1. Get processor for step
         ▼
┌─────────────────┐
│ AdvancedValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 2. processor.execute()
         ▼
┌─────────────────┐
│ EnergyPlus      │
│ Validator       │
└────────┬────────┘
         │
         │ 3. engine.validate()
         │    - Evaluate INPUT-stage assertions
         │    - backend = DockerComposeExecutionBackend
         │    - backend.execute() → Runs container, BLOCKS
         │    - Returns ValidationResult with output_envelope
         │
         ▼
┌─────────────────┐
│ AdvancedValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 4. persist_findings(input_stage_issues)
         │ 5. result.passed is NOT None (sync!)
         │
         ▼
┌─────────────────┐      ┌─────────────────┐
│ _complete_with_ │      │ EnergyPlus      │
│ envelope()      │─────▶│ Validator       │
└────────┬────────┘      └────────┬────────┘
         │                        │
         │                        │ 6. engine.post_execute_validate()
         │                        │    - Extract signals from envelope
         │                        │    - Evaluate OUTPUT-stage assertions
         │                        │    - Return ValidationResult with signals
         │                        │
         │◀───────────────────────┘
         │
         │ 7. persist_findings(output_stage_issues)
         │ 8. store_signals(signals)
         │ 9. store_assertion_counts(combined)
         │ 10. finalize_step(status, stats)
         │
         ▼
┌─────────────────┐
│ StepProcessing  │
│ Result          │
│ (passed=True)   │
└─────────────────┘
```

### Flow 3: Advanced Validator - Async (GCP Cloud Run)

When running on GCP, containers are launched asynchronously and report back via callback.

**Phase 1: Launch Container**
```
┌─────────────────┐
│ ValidationRun   │
│ Service         │
└────────┬────────┘
         │
         │ 1. Get processor for step
         ▼
┌─────────────────┐
│ AdvancedValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 2. processor.execute()
         ▼
┌─────────────────┐
│ EnergyPlus      │
│ Validator       │
└────────┬────────┘
         │
         │ 3. engine.validate()
         │    - Evaluate INPUT-stage assertions
         │    - backend = GCPExecutionBackend
         │    - backend.execute() → Triggers Cloud Run Job
         │    - Returns IMMEDIATELY with passed=None
         │
         ▼
┌─────────────────┐
│ AdvancedValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 4. persist_findings(input_stage_issues)
         │ 5. result.passed IS None (async!)
         │ 6. _record_pending_state()
         │
         ▼
┌─────────────────┐
│ StepProcessing  │
│ Result          │
│ (passed=None)   │ ◀─── Run stays RUNNING, waiting for callback
└─────────────────┘
```

**Phase 2: Callback Processing (minutes later)**
```
┌─────────────────┐
│ Cloud Run Job   │
│ (EnergyPlus)    │
└────────┬────────┘
         │
         │ 1. Container completes
         │    - Writes output envelope to GCS
         │    - POSTs callback to Django
         │
         ▼
┌─────────────────┐
│ ValidationCallback│
│ Service         │
└────────┬────────┘
         │
         │ 2. Download output envelope from GCS
         │ 3. Get processor for step
         ▼
┌─────────────────┐
│ AdvancedValidation│
│ Processor       │
└────────┬────────┘
         │
         │ 4. processor.complete_from_callback(output_envelope)
         │
         ▼
┌─────────────────┐
│ _complete_with_ │
│ envelope()      │
└────────┬────────┘
         │
         │ 5. Get existing finding counts (INPUT-stage preserved!)
         │ 6. engine.post_execute_validate()
         │ 7. persist_findings(output_issues, append=True)  ◀─── APPEND, not replace!
         │ 8. store_signals(signals)
         │ 9. store_assertion_counts(combined)
         │ 10. finalize_step(status, stats)
         │
         ▼
┌─────────────────┐
│ StepProcessing  │
│ Result          │
│ (passed=True)   │
└─────────────────┘
         │
         ▼
┌─────────────────┐
│ Finalize run or │
│ resume next step│
└─────────────────┘
```

## Assertion Evaluation

### What Are CEL Assertions?

CEL (Common Expression Language) assertions allow users to define custom pass/fail conditions beyond the basic validation logic. For example:

```cel
# Input-stage assertion (runs before container)
submission.metadata.version >= "2.0"

# Output-stage assertion (runs after container completes)
output.metrics.site_eui_kwh_m2 < 100
```

### Two Assertion Stages

| Stage | When Evaluated | Available Data | Applies To |
|-------|----------------|----------------|------------|
| Input | During `engine.validate()` | Submission content, metadata | All validators |
| Output | During `engine.post_execute_validate()` | Container output, signals, metrics | Advanced validators only |

### Assertion Evaluation Happens in Validators

A key design decision: **validators evaluate assertions, not processors**.

Why?
1. Validators know how to extract the assertion payload from their specific data structures
2. Some validators (Basic, AI) were already evaluating assertions in `validate()`
3. Keeps the processor focused on lifecycle, not logic

```python
# Inside JsonSchemaValidator.validate():
result = self._run_schema_validation(submission)
assertion_findings = self.evaluate_cel_assertions(
    payload=parsed_json,
    stage="input",
    run_context=run_context,
)
return ValidationResult(
    passed=result.passed,
    issues=result.issues + assertion_findings,
    assertion_stats=AssertionStats(total=N, failures=M),
)
```

## Signals and Cross-Step Communication

### What Are Signals?

Signals are metrics extracted from validation outputs that can be used by downstream steps. For example, an EnergyPlus step might extract:

```json
{
  "site_eui_kwh_m2": 87.5,
  "site_electricity_kwh": 12500,
  "site_natural_gas_kwh": 8200
}
```

A downstream step can then reference these signals in its assertions:

```cel
# In a subsequent step's output-stage assertion
upstream["energyplus_step"].signals.site_eui_kwh_m2 < 100
```

### Signal Flow

1. **Extraction**: Validator extracts signals during `post_execute_validate()`
2. **Return**: Validator returns signals in `ValidationResult.signals`
3. **Storage**: Processor calls `store_signals()` to persist in `run.summary`
4. **Access**: Downstream steps access via `run_context.downstream_signals`

## File Structure

```
validibot/validations/services/step_processor/
├── __init__.py          # Package exports: get_step_processor
├── base.py              # ValidationStepProcessor abstract base class
├── simple.py            # SimpleValidationProcessor
├── advanced.py          # AdvancedValidationProcessor
├── factory.py           # get_step_processor() factory function
└── result.py            # StepProcessingResult dataclass
```

## Key Classes and Methods

### StepProcessingResult

The return type from all processor `execute()` methods:

```python
@dataclass
class StepProcessingResult:
    passed: bool | None      # None = async, waiting for callback
    step_run: ValidationStepRun
    severity_counts: Counter  # {Severity.ERROR: 2, Severity.WARNING: 5}
    total_findings: int
    assertion_failures: int
    assertion_total: int
```

### ValidationStepProcessor (Base)

Shared methods used by both subclasses:

| Method | Purpose |
|--------|---------|
| `_get_engine()` | Get validator instance from registry |
| `_build_run_context()` | Build context with downstream signals |
| `persist_findings()` | Save ValidationFinding records |
| `store_signals()` | Store signals in run.summary |
| `store_assertion_counts()` | Save assertion stats for run summary |
| `finalize_step()` | Set ended_at, duration_ms, status, output |

### SimpleValidationProcessor

```python
def execute(self) -> StepProcessingResult:
    engine = self._get_engine()
    result = engine.validate(...)
    self.persist_findings(result.issues)
    self.store_assertion_counts(...)
    self.finalize_step(status, stats)
    return StepProcessingResult(passed=result.passed, ...)
```

### AdvancedValidationProcessor

```python
def execute(self) -> StepProcessingResult:
    engine = self._get_engine()
    result = engine.validate(...)  # May launch container
    self.persist_findings(result.issues)  # Input-stage findings

    if result.passed is None:
        # Async - container launched, waiting for callback
        self._record_pending_state(result)
        return StepProcessingResult(passed=None, ...)
    else:
        # Sync - container completed
        return self._complete_with_envelope(engine, result.output_envelope, ...)

def complete_from_callback(self, output_envelope) -> StepProcessingResult:
    # Called by ValidationCallbackService after async completion
    return self._complete_with_envelope(engine, output_envelope, append_findings=True)
```

## Error Handling

Each processor handles errors gracefully:

1. **Validator not found**: Returns `StepProcessingResult(passed=False)` with error finding
2. **Validation exception**: Catches exception, creates error finding, finalizes step as FAILED
3. **Missing envelope (sync)**: Creates error finding explaining configuration issue

All error paths ensure:
- A `ValidationFinding` with severity ERROR is created
- The step is finalized with status FAILED
- The error message is stored in `step_run.error`

## Testing

### Unit Tests

Tests for processor classes use mocked validators:

```python
def test_simple_processor_passes_on_valid():
    """Test SimpleValidationProcessor with passing validation."""
    mock_engine = Mock()
    mock_engine.validate.return_value = ValidationResult(passed=True, ...)

    processor = SimpleValidationProcessor(run, step_run)
    result = processor.execute()

    assert result.passed is True
    assert step_run.status == StepStatus.PASSED.value
```

### Integration Tests

Full workflow tests verify end-to-end behavior:

- JSON Schema validation with CEL assertions
- EnergyPlus sync execution (Docker Compose)
- Callback flow with mocked async backend
- Input/output-stage assertion preservation

## Related Documentation

- [Workflow Orchestration Architecture](workflow_engine.md) - Higher-level orchestration
- [Validator Architecture](validator_architecture.md) - Execution backends and deployment
- [How Validibot Works](how_it_works.md) - End-to-end system overview
