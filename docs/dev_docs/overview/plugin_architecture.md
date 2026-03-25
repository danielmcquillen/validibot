# Plugin Architecture

Validibot has two extension systems that matter most when you are changing workflow behavior:

- **validator plugins** define what a workflow step can validate
- **action plugins** define what a workflow step can do after validation logic runs

These two systems are intentionally similar. The shared pattern comes first, and then each plugin type adds its own details.

## The shared pattern

At a high level, both validators and actions follow the same lifecycle:

1. A Django app provides a **declarative definition** for the plugin.
2. The app registers that definition during `AppConfig.ready()`.
3. A sync step turns the Python declaration into database rows the UI can work with.
4. Runtime code uses the shared registry to resolve the concrete class, form, or handler it needs.

That split is important. Python declarations are the source of truth for behavior. Database rows are the source of truth for what the UI should show and what admins can activate.

The result is a system that works well for both community code and commercial packages:

- the community repo owns the shared contracts and registries
- `validibot-pro` and `validibot-enterprise` can register their own plugins
- the step editor and runtime do not need special-case imports for each commercial feature

## Common ideas

The names differ a little between validators and actions, but the moving parts are the same.

| Concept | Validators | Actions |
| --- | --- | --- |
| Declarative definition | `ValidatorConfig` | `ActionDescriptor` |
| Startup entry point | `ValidationsConfig.ready()` | `ActionsConfig.ready()` plus any installed commercial app's `ready()` |
| Runtime registry purpose | resolve validator classes and metadata | resolve action models, forms, handlers, and metadata |
| Database sync target | `Validator`, `SignalDefinition`, `Derivation` | `ActionDefinition` |
| Main sync command | `sync_validators` | `seed_default_actions` or `setup_validibot` |

The main difference is that validators carry more catalog metadata than actions. A validator declaration does not just say "this validator exists." It also says what signals and derivations it exposes, which file types it supports, and which optional editor cards it adds. Action declarations are simpler. They mainly identify the action, its feature gate, and the runtime classes needed to edit and execute it.

## Validator plugin mechanism

Validators use `ValidatorConfig` as their single source of truth. You can see that contract in `validibot/validations/validators/base/config.py`.

Community validators are loaded in two ways:

- **package-based validators** live under `validibot.validations.validators.<name>` and expose a `config.py`
- **single-file built-ins** are declared in the built-in config list and loaded alongside the package-based validators

At startup, `ValidationsConfig.ready()` calls `populate_registry()`. That function discovers every `ValidatorConfig`, resolves the dotted class paths, and fills the in-memory registries used by the runtime.

After startup, `sync_validators` turns those Python declarations into database rows:

- `Validator`
- `SignalDefinition`
- `Derivation`

That is why a new validator usually requires both halves:

1. declare the validator in Python
2. sync the database rows

The editor and runtime then use the same declaration for several different jobs:

- the workflow editor can list the validator
- the signal catalog can show its declared inputs and outputs
- the runtime can instantiate the validator class
- optional step-editor cards can be injected without hard-coding template logic in the core app

## Action plugin mechanism

Actions now follow the same broad model, but with an `ActionDescriptor` instead of `ValidatorConfig`. The shared action registry lives in `validibot/actions/registry.py`.

Community actions are registered from `validibot.actions.registrations` during `ActionsConfig.ready()`. Commercial packages follow the same pattern from their own Django apps. For example, `validibot-pro` registers its signed credential action from its own `AppConfig.ready()` method rather than relying on community code to host the concrete model, form, or handler.

An action descriptor supplies the information the host app needs:

- slug, name, description, and icon
- action category and type
- optional `required_feature`
- concrete model class
- form class for the workflow step editor
- runtime handler class

`create_default_actions()` and `seed_default_actions` turn those registered descriptors into `ActionDefinition` rows. The `setup_validibot` command also runs that sync step as part of initial environment setup.

At runtime, action resolution is split in a similar way to validators:

- the step picker reads active `ActionDefinition` rows
- the editor uses the action registry to resolve the form class
- the step orchestrator uses the action registry to resolve the execution handler

This means a commercial action can stay fully inside `validibot-pro` while still appearing naturally in the community-hosted workflow UI.

## Official plugin guards

Both plugin systems are deliberately conservative about who may register plugins.

By default, only providers from the official package namespaces are allowed:

- `validibot`
- `validibot_pro`
- `validibot_enterprise`

The two settings are:

- `VALIDIBOT_ALLOWED_VALIDATOR_PLUGIN_PREFIXES`
- `VALIDIBOT_ALLOWED_ACTION_PLUGIN_PREFIXES`

A self-host operator can widen either allowlist intentionally, but an unexpected third-party package will not register silently by default.

## How to add a new validator plugin

Adding a validator usually means:

1. Add or update the `ValidationType` constant.
2. Implement the validator class.
3. Declare a `ValidatorConfig`.
4. Add any catalog entries or step-editor cards the validator needs.
5. Run `sync_validators`.

If the validator is a community feature, place it under `validibot.validations.validators`. If it is a future commercial-only validator, keep the same declarative pattern and make sure the loading story stays explicit rather than import-by-side-effect. If it lives outside the official package namespaces, add that provider prefix to `VALIDIBOT_ALLOWED_VALIDATOR_PLUGIN_PREFIXES`.

## How to add a new action plugin

Adding an action follows the same shape, but the concrete classes usually live together:

1. Create the action model.
2. Create the workflow-step form.
3. Create the runtime handler.
4. Register an `ActionDescriptor` from the owning app's `AppConfig.ready()`.
5. Run `seed_default_actions` or `setup_validibot`.

For commercial actions, keep the concrete implementation in the commercial package. Community code should own the registry contract and orchestration hooks, not the Pro-only business logic. If an action plugin lives outside the official package namespaces, add that provider prefix to `VALIDIBOT_ALLOWED_ACTION_PLUGIN_PREFIXES`.

## Why this split matters

This architecture keeps open-core boundaries clean.

Community Validibot knows how to host plugins, show synced definitions in the workflow editor, and execute registered handlers. It does not need to contain the full implementation of every commercial step type.

That gives us a few practical benefits:

- commercial packages can own their own models, migrations, templates, and handlers
- the workflow editor can stay generic
- startup is deterministic because each installed app registers its own plugins in one place
- self-host operators can control which plugin namespaces are allowed to register

## Related docs

- [Commercial Extensions](commercial_extensions.md)
- [Service Architecture](service_architecture.md)
- [How It Works](how_it_works.md)
