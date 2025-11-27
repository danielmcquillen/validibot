# ADR-2025-11-25: Issue Validation Credentials as VC 2.0

**Status:** Proposed (2025-11-25)  
**Owners:** Platform / Validations  
**Related ADRs:** 2025-10-31-ruleset-and-provider, 2025-11-07-assertion-creation-format, 2025-11-16-CEL-implementation  
**Related docs:** `dev_docs/how-to/configure-jwks.md`, `dev_docs/data-model/steps.md`

---

## Context

SimpleValidations already defines action-based workflow steps for non-validation side effects (Slack messages, certificates, etc.) via concrete subclasses of `Action`. The current set includes:

- `SlackMessageAction` – posts a Slack message when a run completes.
- `SignedCertificateAction` – stores an uploaded PDF template and issues a signed certificate artifact after a run.

However, `SignedCertificateAction` is largely a placeholder:

- It doesn’t yet define a standard machine-readable credential format.
- It doesn’t integrate with the badge JWKS infrastructure that now exposes KMS-backed public keys at `/.well-known/jwks.json`.
- It is focused on PDF certificates and doesn’t model more general “credentials about data” (e.g. badges, attestations about an IDF/EPJSON/FMU submission, etc.).

We want an MVP feature that:

1. Lets workflow authors add a step that awards a verifiable credential when a submission passes validation.
2. Issues credentials that are:
   - Cryptographically signed, using our existing AWS KMS + JWKS setup.
   - Standardised so they can be verified without depending on private implementation details.
   - Tied to the submission data (e.g. hash of the uploaded file, workflow, run, org).
3. Is small enough to ship by the January alpha, without dragging in full-blown wallet protocols, DID infrastructure, or blockchain anchoring.

Future requirements:

- We will later want other Actions (e.g. blockchain anchor, sending results to external systems) to reuse the same “this run is attested” primitive.
- We may want per-organization issuers and different credential flavours (e.g. Open Badges for courses vs. validation attestations for models).

---

## Decision

We will:

1. **Introduce a single canonical Action `IssueValidationCredentialAction`**

   This replaces `SignedCertificateAction` as the main engine for issuing attestations about validation runs. It issues one canonical Verifiable Credential (VC) per successful run, with optional human-friendly artifacts (certificate PDF, badge PNG) layered on top.

2. **Represent credentials using the W3C Verifiable Credentials Data Model 2.0**

   - Each credential is a VC 2.0 document whose subject is a validation of a specific submission.
   - The `credentialSubject` will include:
     - A stable identifier for the submission or run.
     - A hash of the validated content (e.g. SHA256 of the uploaded file or canonicalised payload).
     - Workflow and validator metadata (workflow ID, version, validators used, completion time, status).
   - No personal identity / PII is involved for this MVP; we are attesting to data and its validation, not a person.

3. **Secure credentials using JOSE/JWS, signed by AWS KMS and advertised via JWKS**

   - Use the existing badge-signing KMS key (`KMS_KEY_ID`) and the JWKS endpoint (`/.well-known/jwks.json`).
   - Represent each VC as a JWS:
     - Header: `alg` (e.g. `ES256`), `kid`, `typ` (e.g. `vc+ld+jwt`).
     - Payload: the VC 2.0 JSON document.
   - Use AWS KMS to perform the actual signing; wrap the result in JWS using a JOSE library.
   - Publish public keys and `kid` values in JWKS so external verifiers can validate signatures offline.

4. **Add an `IssuedCredential` model to persist credentials**

   - Each `IssuedCredential` links to:
     - `organization`
     - `project` (optional)
     - `submission`
     - `ValidationRun` and `ValidationStepRun`
     - `IssueValidationCredentialAction` configuration
   - It stores:
     - `vc_payload` (JSON) – the unsigned VC 2.0 payload.
     - `vc_jws` (text) – the signed JWS string.
     - `issuer_id` – URI string representing the issuer (platform-level for MVP).
     - `kid` – key identifier matching the JWKS entry used to sign.
     - `status` – `"active"` or `"revoked"`.
     - `format_version` – e.g. `"vc2-jws-v1"`.
   - This creates a stable, queryable record of all credentials independent of how we sign them in future.

5. **Define a simple credential schema for validation results**

   MVP VC structure (illustrative):

   ```json
   {
     "@context": [
       "https://www.w3.org/ns/credentials/v2",
       "https://www.simplevalidations.com/credentials/validation/v1"
     ],
     "type": ["VerifiableCredential", "ValidationCredential"],
     "issuer": "https://www.simplevalidations.com/issuers/platform",
     "issuanceDate": "2025-11-25T12:34:56Z",
     "credentialSubject": {
       "id": "urn:sv:submission:<submission_ulid>",
       "type": ["ValidationSubject", "Submission"],
       "resource": {
         "id": "urn:sv:file:<file_ulid>",
         "hash": "urn:sha256:<hex>",
         "mediaType": "application/ep+json",
         "filename": "foo.epJSON"
       },
       "validation": {
         "workflowId": "<workflow_id>",
         "workflowSlug": "baseline-epjson",
         "workflowVersion": 3,
         "runId": "<run_ulid>",
         "status": "passed",
         "completedAt": "2025-11-25T12:34:56Z",
         "validators": [
           {
             "code": "eplus-basic",
             "version": "1.0.0",
             "validationType": "ENERGYPLUS_IDF"
           }
         ]
       }
     },
     "credentialStatus": {
       "id": "https://www.simplevalidations.com/credentials/status/<credential_ulid>",
       "type": "SimpleValidationsStatusList2025"
     }
   }
   ```

   - `credentialStatus` is reserved for later; in the MVP it is effectively always “active” unless we mark the credential as revoked in our DB.
   - `issuer` is platform-level in the MVP; we will add per-organization issuers later without changing the credential subsystem.

6. **Make `IssueValidationCredentialAction` the single source of truth for certificates/badges**

   - `IssueValidationCredentialAction` is a new concrete `Action` subclass with (MVP) configuration fields like:
     - `display_format` – enum: `"CERTIFICATE"` or `"BADGE"` (drives human-facing artifact only).
     - `issue_on_warnings` – boolean (issue even if run has warnings).
     - `subject_type` – `"submission"` (MVP; may later support `"project"` or custom IDs).
   - It always issues one canonical VC and stores it in `IssuedCredential`.
   - It may additionally trigger an artifact renderer (PDF, PNG) based on `display_format`.

7. **Treat `SignedCertificateAction` as a thin compatibility wrapper**

   - Existing workflows that reference `SignedCertificateAction` continue to work.
   - Internally, `SignedCertificateAction` is implemented as a wrapper that:
     - Delegates to the same issuing service as `IssueValidationCredentialAction`, with `display_format="CERTIFICATE"`.
     - Stores any uploaded certificate template as a rendering hint when generating the PDF.
   - New workflows in the UI will only expose `IssueValidationCredentialAction`; `SignedCertificateAction` will be hidden/marked as legacy.

8. **Triggering and idempotency rules**

   - The `IssueValidationCredentialAction` step executes after all prior validator steps.
   - The action will issue a credential only if the overall `ValidationRun` status is:
     - `"PASSED"` or
     - `"PASSED_WITH_WARNINGS"` (if `issue_on_warnings=True`).
   - If a credential has already been issued for the given run + step (e.g. due to retries), the step is idempotent and simply returns the existing `IssuedCredential`.
   - If the run fails, the step is marked `skipped` and no credential is generated.

9. **Verification endpoints**

   We will expose two main endpoints:

   1. `GET /credentials/<credential_ulid>/`

      - HTML:
        - Displays “Valid / Revoked / Unknown” status.
        - Shows high-level metadata: organization, project, workflow, run ID, submission hash, issuance date.
        - Shows links back to the run/submission if the viewer is authenticated and has permission; otherwise redacts sensitive details.
      - JSON (`Accept: application/json`):
        - Returns `vc_payload`, `vc_jws`, `status`, and a basic verification result.

   2. `POST /credentials/verify/`
      - Accepts either:
        - a `credential_id` referencing an `IssuedCredential`, or
        - a raw JWS string.
      - Performs:
        - JWS header decode → extract `kid`.
        - Fetch public key from JWKS (or cached keys).
        - Verify signature against `vc_payload`.
        - Look up the `IssuedCredential` record (if present) and include `"active"` / `"revoked"` / `"unknown"` status.
        - If the original submission content is still available, optionally recompute the hash and confirm it matches `credentialSubject.resource.hash`.

10. **Minimal MVP scope**

For the January alpha, we explicitly do not implement:

- Per-organization issuer keys; we use a single platform-level issuer backed by one KMS key.
- Wallet protocols (OID4VCI, OID4VP), DID resolution, or SD-JWT VC.
- Fine-grained revocation lists; we only support a simple `status` field on `IssuedCredential` (toggled via admin UI if needed).
- Blockchain anchoring; this will be a separate Action that can later consume `IssuedCredential` entries and anchor their hashes.

---

## Implementation Sketch

### Data model

1. **New `IssueValidationCredentialAction` model**

   ```python
   class CredentialDisplayFormat(models.TextChoices):
       CERTIFICATE = "CERTIFICATE", "Certificate (PDF)"
       BADGE = "BADGE", "Badge (PNG)"

   class IssueValidationCredentialAction(Action):
       display_format = models.CharField(
           max_length=20,
           choices=CredentialDisplayFormat.choices,
           default=CredentialDisplayFormat.CERTIFICATE,
       )
       issue_on_warnings = models.BooleanField(default=False)

       subject_type = models.CharField(
           max_length=50,
           choices=[("submission", "Submission")],
           default="submission",
       )

       # Optional template for PDF certificates (for CERTIFICATE format)
       certificate_template = models.FileField(
           upload_to="cert_templates/",
           null=True,
           blank=True,
       )

       format_version = models.CharField(
           max_length=20,
           default="vc2-jws-v1",
       )
   ```

2. **New `IssuedCredential` model**

   ```python
   class IssuedCredential(models.Model):
       ulid = ULIDField(primary_key=True, editable=False)

       organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
       project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL)
       submission = models.ForeignKey(Submission, null=True, blank=True, on_delete=models.SET_NULL)
       workflow_run = models.ForeignKey(ValidationRun, on_delete=models.CASCADE)
       step_run = models.ForeignKey(ValidationStepRun, on_delete=models.CASCADE)
       action = models.ForeignKey(IssueValidationCredentialAction, on_delete=models.PROTECT)

       vc_payload = models.JSONField()
       vc_jws = models.TextField()

       issuer_id = models.CharField(max_length=255)
       kid = models.CharField(max_length=128)

       status = models.CharField(
           max_length=20,
           choices=[("active", "Active"), ("revoked", "Revoked")],
           default="active",
       )
       issued_at = models.DateTimeField(auto_now_add=True)
       revoked_at = models.DateTimeField(null=True, blank=True)

       format_version = models.CharField(max_length=20, default="vc2-jws-v1")
   ```

3. **`SignedCertificateAction` wrapper**

   - Keep the existing model and database table.
   - Add a service method that:
     - Creates/uses an `IssueValidationCredentialAction` configuration internally (or calls a shared “issue credential” service with `display_format=CERTIFICATE`).
     - Generates the same `IssuedCredential` entry.
   - Update docs to mark `SignedCertificateAction` as legacy and steer users to `IssueValidationCredentialAction` for new steps.

### Services and libraries

- Add a `credentials` service module, e.g. `simplevalidations/credentials/services.py`, that handles:

  - `build_vc_payload(run, step_run, action)` – builds the VC JSON structure.
  - `sign_vc_payload(vc_payload)` – uses AWS KMS `Sign` API and a JOSE library to produce a JWS string with `kid` referencing the KMS key.
  - `issue_credential(run, step_run, action)` – composes the above, creates `IssuedCredential`, and returns it.

- Reuse existing JWKS code:
  - `simplevalidations.core.jwks.jwk_from_kms_key` to export public key(s).
  - `/.well-known/jwks.json` to publish the current and past signing keys with their `kid`s.

### Execution flow

- During workflow execution, when a step is of type `IssueValidationCredentialAction`:
  1. Check overall `ValidationRun.status`:
     - If failed → mark step `skipped`, do nothing.
     - If passed (or passed with warnings per config) → continue.
  2. If there is already an `IssuedCredential` for this `step_run`, return it and mark the step idempotently `passed`.
  3. Build the VC payload, sign it, persist `IssuedCredential`.
  4. Attach a reference to `IssuedCredential.ulid` into `ValidationStepRun.output` so the run detail page can show a “View credential” link.

---

## Security & Privacy Considerations

Verifiable Credentials in this design provide authenticity and integrity, not confidentiality. We assume that any credential published at a public URL may be retrieved by anyone who knows or discovers that URL.

For the MVP, we adopt these rules:

- VC payloads must be safe to treat as public.
- VC contents must not include personally identifying information about individuals.
- VC contents must not include sensitive customer identifiers (e.g. client names, street addresses) or detailed proprietary performance metrics unless an organization explicitly opts in.
- VC contents should focus on opaque identifiers (ULIDs), hashes of submitted content, media types, workflow identifiers, validator codes, run status, and timestamps.
- Detailed run context (project names, filenames that reveal clients, full metrics, etc.) stays in internal SimpleValidations data models and is only shown to authenticated users with appropriate permissions.
- Public `/credentials/<id>/` endpoints should present a minimal view suitable for sharing and verification; richer information is only available to authenticated users.

## Consequences

**Pros**

- We get a real, standards-based credential rather than an ad hoc PDF:
  - Can be verified independently of SimpleValidations if someone caches the JWKS.
  - Expressive enough for future scenarios (Open Badges 3.0, project-wide attestations).
- We reuse the existing KMS + JWKS infrastructure, so:
  - No custom key management added for MVP.
  - Key rotation is handled via JWKS, and old credentials stay verifiable.
- We centralise all credential issuance logic in one place:
  - `IssueValidationCredentialAction` + `IssuedCredential` + signing service.
  - `SignedCertificateAction` becomes a thin wrapper and can eventually be removed.
- The feature demonstrates clear business value:
  - “Proof” that a model/config passed certain checks at a certain time, tied to the data hash.
  - Easy to extend with future Actions (e.g. blockchain anchoring that consumes `IssuedCredential` rows).

**Cons / trade-offs**

- We add new conceptual weight (VC 2.0, JWS, JWKS) alongside the existing validation concepts.
- We become more tightly coupled to:
  - AWS KMS availability and
  - the correctness of our JWKS endpoint.
- We will need to maintain backwards-compatibility for older credentials when we eventually introduce per-org issuers or new credential formats (e.g. SD-JWT VC, Data Integrity proofs).

---

## Open Questions & Future Work

1. **Per-organization issuers**

   - MVP uses a single `issuer` (`https://www.simplevalidations.com/issuers/platform`).
   - Later, we may introduce:
     - `IssuerKey` model per org.
     - Org-specific issuer IDs and JWKS endpoints (e.g. `https://org.simplevalidations.com/.well-known/jwks.json`).

2. **Additional VC representations**

   - For privacy-focused use cases involving people, we may want SD-JWT VC or Data Integrity.
   - `IssuedCredential.format_version` is designed so we can add more formats without breaking old ones.

3. **Revocation UX**

   - MVP uses a simple `status` field and includes `credentialStatus` in the VC for future expansion.
   - We will later define a clearer revocation workflow (e.g. revoking a credential when a workflow is misconfigured).

4. **Open Badges / course credentials**

   - For learning platforms, we may define a different VC type (e.g. `OpenBadgeCredential`) reusing the same issuance pipeline and KMS keys.

5. **Blockchain anchoring Action**
   - Out of scope for this ADR but expected:
     - A separate `Action` that takes an `IssuedCredential`, hashes it (or uses its embedded hash), and records that hash on a blockchain or notarization service.
     - Links the chain transaction hash back into `IssuedCredential` for auditability.

---
