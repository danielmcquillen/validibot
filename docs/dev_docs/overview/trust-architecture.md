# Trust Architecture

This page describes how Validibot's trust boundaries work — the four invariants every validation run must satisfy, the validator/validator-backend distinction, and how the platform enforces those rules consistently across the web UI, REST API, MCP, and x402 agent paths.

If you're reading this as a developer or self-host operator, the takeaway is: every launch path goes through the same gates, and those gates are services with names you can grep for.

## Why trust matters

A validation run is a fact: "submission X passed workflow Y at time T." For that fact to mean anything later, it has to be backed by:

- the caller was allowed to launch or view the workflow;
- the submitted artifact matched the workflow's declared input contract;
- the exact workflow version was the one intended;
- the validator could not read or mutate unrelated private data;
- the output is tied to hashes, versions, and logs;
- API, CLI, MCP, and x402 paths all behave consistently;
- a result can later be verified by someone who did not run the job.

These are not academic concerns. The risk-averse customers Validibot serves — energy modeling consultancies, utility reviewers, research labs, university instructors — evaluate trust before features. They self-host because their model files and research artifacts cannot leave their environment. Trust defects are the difference between "interesting project" and "acceptable operational tool."

## The four invariants

Every run, regardless of launch channel, must satisfy four invariants before a validator executes. **If any invariant cannot be established, the run does not start.**

### 1. Caller invariant

The authenticated or paid caller is allowed to see and/or execute the specific workflow version.

**Where it's enforced:**

- **`WorkflowAccessResolver`** at `validibot/workflows/services/access.py` — single decision point for "can this user see this workflow?". Combines org-membership, creator role, and active `WorkflowAccessGrant` access. Methods: `list_for_user`, `get_for_user`, `get_or_404`.
- **`AgentWorkflowResolver`** at `validibot/workflows/services/agent_workflows.py` — single decision point for latest-version selection on public agent paths. Methods: `list_published`, `get_by_slug`, x402-relaxed `get_by_slug_for_x402`.

The old pattern `Workflow.objects.filter(org=self.get_org(), is_archived=False)` is allowed only inside the resolver or admin-only code. List endpoints return latest workflow versions unless the endpoint is explicitly versioned. Object-level access is checked **before** serializer selection so full serializers cannot expose fields through accidental broad querysets.

**Guest grants** are workflow-family grants by default (organization plus slug, all non-archived non-tombstoned versions). Grantors who need stricter control can pin a grant to a specific version. New workflow versions do not copy external guest-grant rows during cloning; the resolver interprets the existing family grant unless the grant is explicitly pinned.

### 2. Contract invariant

The submitted artifact is accepted by the workflow and all executable steps that will process it.

**Where it's enforced:** **`LaunchContract`** at `validibot/workflows/services/launch_contract.py`, with `LaunchContractViolation` and a `ViolationCode` enum.

The contract checks:

1. workflow is active and runnable;
2. workflow has steps;
3. caller can execute the workflow;
4. file type is supported by the workflow;
5. each executable validator step supports the selected file type;
6. content type and extension are normalized through one mapping;
7. file size limits are enforced before persistence;
8. suspicious magic bytes are rejected for text-like content;
9. decoded base64 size is bounded before allocation;
10. retention policy is snapped from the workflow/channel;
11. run source is derived from channel, not trusted from user headers.

The web view, REST API, MCP helper API, and x402 run-creation path all consume this contract via the helper `views_helpers.describe_workflow_file_type_violation()`. Channel-specific behavior is expressed as policy, not as a forked implementation. Phase 2 of the trust ADR delivered this convergence with a parity test matrix that pins the wiring at the helper level.

### 3. Isolation invariant

The validator receives only the run-scoped inputs and writable output location needed for that run.

**Where it's enforced:** **`RunWorkspaceBuilder`** at `validibot/validations/services/run_workspace.py`, plus envelope URI rewriting in the Docker dispatch path at `validibot/validations/services/execution/docker_compose.py`.

The per-run workspace layout:

```text
<DATA_STORAGE_ROOT>/runs/<org_id>/<run_id>/
  input/                       # mode 755 — readable by container UID 1000
    input.json                 # mode 644
    <original_filename>        # mode 644 — primary submission file
    resources/                 # mode 755
      <resource_filename>      # workflow resource files (e.g. weather)
  output/                      # owned 1000:1000, mode 770 — writable by UID 1000 only
    output.json                # written by container
    outputs/                   # backend-uploaded artifacts (e.g. eplusout.sql)
```

Container mounts:

| Host path | Container path | Mode |
|---|---|---|
| `runs/<org_id>/<run_id>/input` | `/validibot/input` | read-only |
| `runs/<org_id>/<run_id>/output` | `/validibot/output` | read-write |
| (none) | `/tmp` | tmpfs (`size=2g,mode=1777`) |

The container does **not** receive the global storage root, other run directories, Django media paths, database credentials, signing keys, Stripe/x402 credentials, or arbitrary host directories.

**Default container policy** (Phase 1):

- `network_disabled=True` unless the validator manifest explicitly requires network;
- `cap_drop=["ALL"]`;
- `security_opt=["no-new-privileges:true"]`;
- non-root user (UID 1000);
- read-only root filesystem;
- explicit tmpfs at `/tmp`;
- pids, memory, CPU, and timeout limits;
- container labels for cleanup;
- image pinned by digest when possible.

Validibot's design closely mirrors Pachyderm's `/pfs/<input>` + `/pfs/out`, Flyte's `/var/inputs` + `/var/outputs`, and Cromwell's `/cromwell-executions/...` patterns — fixed in-container paths owned by the orchestrator. We're stricter than the median (Argo, Nextflow, Pachyderm, Airflow, Cromwell all leave network/caps/UID open by default).

### 4. Evidence invariant

The run records enough immutable metadata to explain what happened later.

**Where it's enforced:** **`EvidenceManifestBuilder`** at `validibot/validations/services/evidence.py`, with results persisted in the `RunEvidenceArtifact` model.

The manifest schema is `validibot.evidence.v1`, defined in `validibot-shared` (the package external verifiers can depend on without pulling in the full Django stack). See [Evidence Bundles](evidence-bundles.md) for the full reference.

## The validator vs. validator backend distinction

The four invariants compose because Validibot draws a clear line between two interfaces:

1. **Workflow/step processor → validator.** Internal Validibot interface. Sees broad run context (workflow, ruleset, signals, retention). Owns launch contract enforcement, assertion evaluation, persistence, evidence, retention, and access-control decisions.
2. **Advanced validator → validator backend.** Envelope and execution boundary. Sees the minimum data needed to run the domain tool and return typed outputs.

**The advanced validator is the policy boundary; the validator backend is the compute boundary.**

This distinction is why a validator backend running in a sealed container with no network access can still produce trustworthy results: the orchestrator (an advanced validator) holds all the trust decisions, and the backend just runs the simulation.

See [Terminology](terminology.md) for the full vocabulary.

## Run source: trust the path, not the header

`X-Validibot-Source` may remain as client metadata, but it must not set the trusted run source used for billing, audit, quotas, or evidence. Trusted source is derived from the path.

| Path | Trusted source |
|---|---|
| Browser launch form | `LAUNCH_PAGE` |
| REST API bearer token | `API` |
| CLI token/user agent | `CLI` |
| MCP helper route | `MCP` |
| x402 agent route | `X402_AGENT` |
| Scheduled run | `SCHEDULE` |
| Internal retry/replay | original source preserved |

If callers want to self-identify, that goes into separate `client_name`, `client_version`, or `client_source_hint` fields — never into the trusted source.

## x402 trust gates

x402 proves a payment was made. It does **not** prove:

- the referenced workflow is public;
- the workflow version is latest;
- the submitted file is accepted by the workflow;
- the payment amount matches current price;
- the run should retain data;
- the caller may see prior runs.

x402 run creation must therefore perform:

1. trusted workflow resolution through `AgentWorkflowResolver`;
2. current price comparison;
3. idempotent payment lookup;
4. replay prevention by txhash;
5. launch-contract validation;
6. run creation and enqueue inside one transaction where possible;
7. failure states that do not leave ambiguous paid/no-run outcomes.

MCP clients should perform cheap pre-payment validation where possible (base64 syntax, encoded size, filename presence), but Django remains the source of truth.

## MCP positioning

MCP HTTP/OAuth is the default agent trust path:

- HTTP transport;
- OAuth/API-token authenticated workflows;
- resource-scoped tokens;
- explicit user/org context;
- no local shell execution.

Anonymous public x402 remains a strategic experiment. It does not drive the core architecture before self-hosted trust is solid.

## Threat model

This is not a formal certification boundary. It is a realistic threat model for early production:

**In scope:**

- a guest user with one workflow grant trying to enumerate other workflows;
- an authenticated org user reaching for a workflow, run, or artifact they shouldn't see;
- an anonymous paid x402 agent submitting malformed, oversized, incompatible, or replayed requests;
- a buggy, compromised, or partner-authored validator backend image trying to read other runs, mutate shared storage, open network connections, or emit sensitive data into logs;
- operator mistakes such as broad Docker mounts, unsupported dependency versions, missing backups, or stale validator images;
- semantic drift where a workflow, validator, ruleset, resource file, or validator backend changes while old runs still claim an earlier version.

**Out of scope:**

- root access on the host;
- a compromised database administrator;
- a compromised object-storage administrator;
- a malicious organization owner;
- a stolen signing key;
- a malicious Validibot release.

## Database constraints for trust-critical invariants

Model `clean()` is useful but not enough — direct `QuerySet.update()`, data migrations, admin actions, imports, and test fixtures bypass it. Database constraints capture invariants local to one row:

- x402 billing requires positive `agent_price_cents`;
- public agent discovery requires agent access enabled;
- public agent discovery requires x402 billing mode;
- x402 billing requires `data_retention=DO_NOT_STORE`;
- tombstoned workflows cannot be public agent-discoverable;
- archived workflows cannot be public agent-discoverable;
- submission content is either inline or file, not both;
- purged submissions must have content cleared.

## Three-sided config split for cross-service trust gates

Trust gates that span two services (e.g. the MCP server quotes a price; the Django runtime verifies the receipt) are configured by **three** files, not two. Treat this as a reusable pattern whenever a new feature crosses a service boundary:

| File | Purpose | Lifecycle |
| --- | --- | --- |
| **Public-config file** (`.build`) | The half each side advertises publicly — chain identifiers, asset addresses, facilitator URLs, feature flags. Stamped by the deploy recipe via `--set-env-vars`. | Versioned with code; safe to commit values. |
| **Secret-side file** (`.mcp` or per-service equivalent) | The half each side keeps private — receiving wallets, signing keys, OAuth client secrets. Mounted from Secret Manager. | Per-deployment; rotated independently. |
| **Verifier-side file** (`.django`) | The other side's *copy* of the trust-relevant values, used to verify what arrived against what was expected. | Per-deployment; must agree with the matching values in the other two. |

The third file is the one teams forget. It exists because **the verifier's container does not see the producer's secret mount** — they're independent Cloud Run services with independent secret bindings. The verifier's settings module must declare its own copy of the canonical name, with an optional fallback to the producer-side name for combined deploys (one process holding both halves).

A reusable helper for the verifier side:

```python
# In _cloud_common.py (or equivalent shared settings helper)
def resolve_trusted_value(env, *, canonical: str, fallback: str, default: str = "") -> str:
    """Read ``canonical`` first, then ``fallback`` (treating blank as unset)."""
    val = env.str(canonical, default="").strip()
    if val:
        return val
    val = env.str(fallback, default="").strip()
    if val:
        return val
    return default
```

Properties worth replicating in any cross-service setting:

- **Strict precedence** — canonical wins; fallback only fires when canonical is absent or blank.
- **Blank treated as unset** — env-file templates often ship with placeholder lines (`X=`) that operators forget to fill in. Treating blank as unset lets the fallback take over rather than the canonical's empty string short-circuiting the chain.
- **Fail-closed default** — when both env vars are absent or blank, the fail-closed default kicks in (typically `""`). The verifier refuses every payment / message / receipt until the operator opts in by setting at least one of the two.
- **Whitespace stripped** — copy/paste from a runbook can introduce stray spaces; stripping them removes a footgun.
- **Three-layer test coverage** — (1) helper logic with stubbed envs, (2) source-level grep that the assignment exists in each settings module, (3) live `settings.X` read without `override_settings`. The middle layer is the only one that catches "someone deleted the block from cloud.py."

The x402 payment verification is the canonical example — see `validibot-cloud/validibot_cloud/settings/_cloud_common.py::resolve_x402_pay_to_address`.

## Configuration patterns that close trust gaps

Three architectural patterns recurred across every trust gate added under the trust ADR. Worth knowing as design vocabulary when working on new trust-relevant code:

- **Fail-closed defaults** — empty allow-list rejects, unknown policy value raises, missing config refuses. The opposite (silently fall back to the loosest mode) is exactly the inversion of operator intent that turns hardening into a regression.
- **Conservative suppression** — when a row is in a contradictory state (e.g. `agent_public_discovery=True` + `is_archived=True`), withdraw the *claim* (clear `agent_public_discovery`), not the *state* the operator chose (don't flip `is_archived`, don't flip `input_retention`). Reversible via re-publish; the alternative would silently change the privacy contract.
- **Route-bound trust** — every trust-relevant value (run source, payment recipient, validator-backend digest) derives from the authenticated route, never from a client header. Headers are caller-controlled by definition; the route is what the auth layer already verified.

Each pattern is enforced by tests that pin the exact behaviour. When you find yourself writing `assert result in {...some_values}` because the contract is unclear, that's evidence the underlying decision hasn't been made — pick a value and pin it.

## Audit hooks around trust decisions

Specific events feed the audit log:

- workflow access denied;
- workflow execution denied;
- launch rejected by file-type contract;
- launch rejected by step incompatibility;
- x402 payment accepted;
- x402 payment replayed;
- x402 run creation failed after payment confirmation;
- validator sandbox policy violation;
- evidence bundle exported;
- evidence bundle export omitted raw content due to retention policy.

## User-kind classification + per-org RBAC

The trust model distinguishes two orthogonal axes for "what is this user allowed to do?":

1. **User kind** (`User.user_kind`) — the system-wide classifier: `BASIC` or `GUEST`. A property of the *account*, not of any workflow or org. In community deployments every account is `BASIC` (the GUEST classifier doesn't exist without `validibot-pro`). In Pro deployments, `GUEST` accounts are external collaborators who hold no `Membership` rows and can only see workflows they've been granted access to.

2. **Per-workflow access** — answered by `WorkflowAccessGrant` (per-workflow), `OrgGuestAccess` (org-wide), `Membership` + `OrgPermissionBackend` (member-with-role), and the `is_public` flag (platform-wide). Resolved by `Workflow.objects.for_user(user)` which unions every access path. A `BASIC` user can hold a `WorkflowAccessGrant` for a workflow in another org for cross-org collaboration without becoming `GUEST` — the kind tracks the account, not any single relationship.

### Why the split matters

Throttling, UI navigation, and admin lockdowns key off the **kind** axis: "is this account a guest of the system as a whole?" Read-side queryset narrowing keys off the **per-workflow access** axis: "can this account see this specific row?" Conflating them caused subtle bugs in earlier iterations where a basic user with a single cross-org grant got guest-level rate limits and a stripped-down navigation; the split fixes that.

### Sticky semantics

The `Guests` Django Group is the source of truth for `user_kind`, and changing the classification requires either the `promote_user` management command or its admin-action wrapper. Three structural guards keep the classifier consistent:

- **`Membership.clean()`** refuses to add a `GUEST` user as an organization member. The guard runs in `full_clean`, which `Membership.save` invokes on every write path — direct ORM creates, fixtures, and admin shortcuts all trip it.
- **`UserAdmin.get_form`** disables the `groups` field for non-superuser staff. Bypassing the audited promotion path via Django admin click-throughs is denied at the form layer.
- **`m2m_changed` signal on `User.groups`** records every membership change as a `USER_GROUPS_CHANGED` audit event. The promotion command additionally records intent-specific `USER_PROMOTED_TO_BASIC` / `USER_DEMOTED_TO_GUEST` rows; the generic m2m row is suppressed during those flows so the audit log has exactly one entry per intent.

### Operator kill switches

Two `SiteSettings` booleans give operators run-time control:

- **`allow_guest_access`** — when `False`, the allauth `pre_login` adapter rejects credential-validated `GUEST` users with a flash message redirecting them back to login. Existing accounts are kept; flipping the flag back on restores access. Useful as an incident-response kill switch.
- **`allow_guest_invites`** — when `False`, `GuestInvitesEnabledMixin` returns 403 from BOTH the create endpoints (`GuestInviteCreateView`, `WorkflowGuestInviteView`) AND the accept endpoints (`WorkflowInviteAcceptView`, `AcceptGuestInviteView`). Two-sided enforcement makes the toggle atomic from the operator's perspective — pending invites cannot sneak through during a temporary disable window. Pending rows remain `PENDING` in the database while the flag is `False`; flipping it back on lets unexpired invites be redeemed.

### Both gates compose with per-org RBAC

The site-wide kill switches do NOT replace `OrgPermissionBackend`. The standard flow for guest invite creation is:

1. `GuestInvitesEnabledMixin` checks `allow_guest_invites` (site-wide kill switch).
2. `FeatureRequiredMixin` checks `guest_management` is licensed (Pro feature gate).
3. `OrganizationPermissionRequiredMixin` checks the user has the `GUEST_INVITE` permission on the resolved org (per-org RBAC, granted to ADMIN/AUTHOR/OWNER).

Mixin ordering is left-most first. A 403 from the site-wide gate is more honest than a 404 from the feature gate when the feature *is* licensed but currently disabled.

## See also

- [Terminology](terminology.md) — the full vocabulary
- [Validator Architecture](validator_architecture.md) — the input/output envelope contract that the isolation invariant rewrites for
- [Workflow Versioning](../data-model/workflow-versioning.md) — how the trust contract is preserved across versions
- [Evidence Bundles](evidence-bundles.md) — the manifest schema and retention policy
- [Self-Hosting Overview](../../operations/self-hosting/overview.md) — how trust shows up to the operator
