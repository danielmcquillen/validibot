# How Validibot Works

Validibot validates data by running it through a workflow—an ordered sequence of validation steps.

---

## The Validation Flow

```
Upload → Workflow → Steps → Findings → Pass/Fail
```

1. **You upload a submission** — A file (JSON, XML, text, etc.) or pasted content
2. **The workflow processes it** — Your data goes through each step in order
3. **Validators check the data** — Each step runs a validator with assertions
4. **Findings are recorded** — Issues, warnings, and info are captured
5. **You get a result** — Pass if no errors, Fail if errors were found

---

## Key Components

### Workflow
A reusable definition of what to validate and how. Each workflow:

- Belongs to an organization and project
- Has one or more ordered steps
- Can be active, inactive, or archived

### Steps
Each step runs a validator or action. Steps execute in order from top to bottom. If a step produces an error, subsequent steps may still run (depending on configuration).

### Validators
The engines that check your data:

- **JSON Schema** — Structure validation
- **XML Schema** — XSD validation
- **Basic** — Custom CEL rules
- **AI** — Natural language rules
- **Advanced** — Simulation-based (EnergyPlus, FMU)

### Assertions
Rules evaluated during validation. Each validator has default assertions that always run. You can add step-level assertions for workflow-specific rules.

### Findings
The output of validation. Each finding has:

- **Severity**: Error (blocks pass), Warning, or Info
- **Message**: What was found
- **Path**: Where in your data the issue exists

---

## Tips

- **Match file types**: Make sure your workflow's allowed file types match what your validators support
- **Archive, don't delete**: Archiving preserves run history while preventing new submissions
- **Test with sample data**: Run a test submission before making workflows available to your team
