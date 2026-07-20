# Terminology

This page is the canonical glossary for Validibot's architecture. Use these terms consistently in code, comments, tests, and docs. Many terms became load-bearing once the validator/validator-backend distinction started carrying security and versioning guarantees.

## Validators and the validation pipeline

| Term | Meaning |
|---|---|
| **Validator** | The step-level Validibot component represented by `validations.Validator` and implemented by a `BaseValidator` subclass resolved through `ValidatorConfig`. Receives the full Validibot run context: submission, workflow, step config, assertions, rulesets, resource bindings, retention/reporting context, internal services. |
| **Simple validator** | A `SimpleValidator` subclass that runs synchronously inside Django and returns a complete `ValidationResult` from `validate()`. May still evaluate CEL assertions, emit findings, and produce step outputs, but does not launch an external validator backend. Examples: JSON Schema, XML Schema, Basic/CEL, THERM's structural checks. |
| **Advanced validator** | An `AdvancedValidator` subclass that orchestrates external compute. Validates run context, preprocesses the submission (e.g. EnergyPlus template resolution), builds an `ExecutionRequest`, selects an `ExecutionBackend`, dispatches to a validator backend, processes the output envelope, evaluates output-stage assertions. |
| **Validator backend** | The external domain implementation an advanced validator delegates to. Receives a `validibot-shared` input envelope, performs isolated heavyweight work, returns a typed output envelope. Today: Docker images in `validibot-validator-backends/`. Future: WASM modules, Windows VM jobs, partner-provided containers. |
| **Validator container** / **validator Service request** / **validator Job** | Use only for the concrete runtime shape. A Service deployment may serve sequential requests, but each request runs a fresh one-shot child. |

### Why the validator vs. validator backend distinction matters

There are two interfaces:

1. **Workflow/step processor → validator.** Internal Validibot interface. Sees broad run context. Owns launch contract enforcement, assertion evaluation, persistence, evidence, retention, and access-control decisions.
2. **Advanced validator → validator backend.** Envelope and execution boundary. Sees the minimum data needed to run the domain tool and return typed outputs.

**The advanced validator is the policy boundary; the validator backend is the compute boundary.** The advanced validator may see more data than its validator backend. That's intentional.

## Workflow values and step I/O

The namespace determines the correct term. A value is not a signal merely
because a validator parses, consumes, or produces it.

| Term | Meaning |
|---|---|
| **Workflow signal** | An author-named CEL/JSON value in the workflow vocabulary, exposed as `s.<name>` / `signal.<name>`. A `WorkflowSignalMapping` resolves a signal from submission data before the run's steps begin. Promotion can add a value-port step input or step output to the same namespace for later steps; artifacts can never be signals. |
| **Step I/O definition** | A `StepIODefinition` row declaring one input or output port in a validator or workflow-step contract. It may describe a small value or an artifact reference. The row is not itself a signal. |
| **Step input** | A value or artifact consumed by one step. Small values are exposed to that step as `i.<contract_key>` / `input.<contract_key>` and may come from parser facts or a `StepInputBinding`. |
| **Step output** | A value or artifact produced by one step. Small values are exposed to that step as `o.<contract_key>` / `output.<contract_key>`. Completed upstream values remain available as `steps.<step_key>.output.<contract_key>`. |
| **Step input binding** | A `StepInputBinding` row connecting a step input to submission data, a workflow signal, a workflow constant, an upstream step value or artifact, a workflow resource, or a system source. |
| **Promotion** | The explicit act of giving a small step input/output value a `promoted_signal_name`. The value keeps its step-local identity and also becomes `s.<promoted_signal_name>` for downstream steps after the producing stage completes. |
| **Workflow constant** | An author-defined literal in `c.<name>` / `const.<name>`. Constants are part of the workflow contract but are not signals because they are not resolved from run data. |
| **Django signal** | A framework event hook such as `post_save` or `user_logged_in`. This is unrelated to the Validibot workflow-signal vocabulary; Django's established term remains correct. |

Use **signal** only for a value that actually appears in `s.*`, for the
configuration that creates such a value, or for a Django framework signal.
Use **step input**, **step output**, **step I/O definition**, or **port** for
validator contract entries and the values they carry. An input/output becomes
a workflow signal only after explicit promotion.

## Execution and dispatch

| Term | Meaning |
|---|---|
| **Execution deployment** | A verified provider route beneath a versioned validator. Managed attempts pin its exact revision, URL/resource, image digest, runtime identity, capabilities, and timeout/capacity facts before provider contact. |
| **Execution backend** | The platform adapter selected from the pinned deployment: `DockerComposeExecutionBackend`, `CloudRunServiceExecutionBackend`, or `CloudRunJobsExecutionBackend`. |
| **Validator runner / provider dispatcher** | Lower-level launch mechanism: local Docker, Cloud Run Jobs API, or deterministic Cloud Tasks HTTP delivery to a private Service. |
| **Validator backend runtime** | The concrete thing launched for one run: a Docker container, Cloud Run Service request/child, Cloud Run Job, Cloud Batch job, or future execution unit. Receives only the narrow attempt envelope and capability. |

The relationship: an advanced validator *has* a validator backend, while an execution backend *runs* that backend on Docker, Cloud Run, Cloud Batch, or a future platform.

### What the runtime does NOT receive

- the global storage root;
- other run directories;
- Django media paths;
- database credentials;
- signing keys;
- Stripe/x402 credentials;
- arbitrary host directories.

## Reserved word: "engine"

Reserve **engine** for domain software where that is the natural term:

- the EnergyPlus simulation engine;
- the FMU runtime;
- the CEL evaluation engine;
- the database engine.

Do **not** introduce "engine" as a new architecture term. Older docs and tests sometimes used "engine" to mean a validator instance, a simulation runtime, or the workflow orchestration layer; new code should use the precise vocabulary above.

## Trust boundary terms

| Term | Meaning |
|---|---|
| **Caller invariant** | The authenticated or paid caller is allowed to see and/or execute the specific workflow version. Enforced by `WorkflowAccessResolver` and `AgentWorkflowResolver`. |
| **Contract invariant** | The submitted artifact is accepted by the workflow and all executable steps that will process it. Enforced by `LaunchContract`. |
| **Isolation invariant** | The validator receives only the run-scoped inputs and writable output location needed for that run. Enforced by `RunWorkspaceBuilder` plus envelope URI rewriting in the Docker dispatch path. |
| **Evidence invariant** | The run records enough immutable metadata to explain what happened later. Enforced by `EvidenceManifestBuilder` and `RunEvidenceArtifact`. |
| **Trusted source** | The launch channel the run actually came through (`LAUNCH_PAGE`, `API`, `CLI`, `MCP`, `X402_AGENT`, `SCHEDULE`). Derived from the path, never from a client header like `X-Validibot-Source`. |
| **Validator backend trust tier** | First-party (current Phase 1 hardening: UID 1000, cap_drop ALL, network disabled, ro input mount, rw output mount, tmpfs `/tmp`) vs. user-added (tier 1 + egress allowlist, tighter resource caps, gVisor/Kata when available, cosign-signed image required, pre-flight scan). The `Validator.trust_tier` field selects the runner profile. |

## Workflow versioning terms

| Term | Meaning |
|---|---|
| **Locked workflow** | A workflow where `requires_new_version_for_contract_edits()` returns true: has runs, has submissions, is_locked is true, has issued credentials, or `x402_enabled=True`. Contract edits are blocked until a new version is created. |
| **Contract field** | A field whose value affects what a future validation means. Listed in `CONTRACT_FIELDS`. Cannot be edited in place once the workflow is locked. |
| **Semantic digest** | SHA-256 of the canonicalised JSON of a `Validator`'s behavior-defining fields. Stored on `Validator.semantic_digest`. `sync_validators` raises if the digest changes under the same `(slug, integer version)` (drift detection). |
| **Content hash** | SHA-256 of a resource file's bytes, stored on `ValidatorResourceFile.content_hash` and `WorkflowStepResource.content_hash`. Drift detection: `save()` raises if the hash differs and the row is referenced by a locked workflow. |
| **Legacy versioning** | A locked workflow whose `semantic_digest` or `content_hash` columns are unpopulated, either because the row predates digest/hash enforcement or because it uses a custom validator (no source-of-truth config to digest against). The audit command surfaces these as `*_MISSING` findings. |
| **Workflow family** | All non-archived, non-tombstoned versions of a workflow with the same `(org, slug)`. Guest grants apply at family scope: any active grant on any version of a family authorises every version of that family. Per-version pinned grants are a planned enhancement and not currently implemented. |

## Repo and deployment terms

| Term | Meaning |
|---|---|
| **Local** | The developer testing stack at `docker-compose.local.yml`, driven by `just local <cmd>`. Single user, dev stage only. |
| **Self-hosted** | The customer-operated stack at `docker-compose.production.yml`, driven by `just self-hosted <cmd>`. Single VM, single environment. Audience: customers running on DigitalOcean, AWS EC2, Hetzner, on-prem. |
| **GCP** | Validibot's hosted offering on Cloud Run, Cloud SQL, Cloud Tasks, GCS. Driven by `just gcp <cmd>`. Multi-stage (dev/staging/prod). Audience: Validibot team. |
| **Validator backend repo** | `validibot-validator-backends` (renamed from `validibot-validators` in March 2026). Houses Docker images that implement validator backends. The Python package and Docker image prefixes match the new name. |
| **Profile** | A combination of (target, stage, edition) that controls doctor-check severity, feature gating, and defaults. Examples: `local-dev`, `local-eval`, `self-hosted`, `self-hosted-hardened`, `gcp`, `gcp-staging`. |

## See also

- [Workflow Data Architecture](workflow_data_architecture.md) — how namespaces, step I/O, signals, and artifacts fit together
- [Validator Architecture](validator_architecture.md) — the input/output envelope contract
- [Execution Backends](execution_backends.md) — how dispatch to Docker vs. Cloud Run is selected
- [Trust Architecture](trust-architecture.md) — the four trust invariants and how they compose
- [Workflow Versioning](../data-model/workflow-versioning.md) — the trust contract for workflow rules
- [Deployment Overview](../deployment/overview.md) — how local, self-hosted, and GCP relate
