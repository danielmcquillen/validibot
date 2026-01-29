## Validators overview

Validators are the engines that check submissions inside workflow steps.

- **Built-in validators**: EnergyPlus IDF, XML Schema, FMI, and others. Each ships with default assertions that always run.
- **Custom validators**: You can clone a validator template and add your own defaults or schema. Authors can edit their own; Admins/Owners can edit any.
- **Default assertions**: Shown at the top of the step editor and in the validator detail. They always run when that validator is used.
- **Step assertions**: For advanced validators, add CEL-based assertions per step to tighten checks for a specific workflow.
- **File type support**: The selector shows all validators; ones that do not match the workflow’s submission types are disabled with a hint.

Tip: keep validator versions stable. Create a new version rather than overwriting defaults if you need a breaking change.
