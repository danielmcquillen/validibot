# ADR: FMI Storage Strategy and Security Review Workplan

**Status:** Proposed (2025-11-20)  
**Related ADRs:** 2025-11-17-FMI-Validator, 2025-11-17-FMI-Validator-update

## Context

We now support creating FMI validators that ingest user-uploaded FMUs. The initial implementation fetched FMUs from presigned URLs at run-time, which added moving parts (URL lifetime, bearer-token handling) and repeated downloads. We need a clearer storage strategy, better performance for repeated runs, and an explicit security review focused on FMU lifecycle and execution isolation.

## Decision

1. **Store every uploaded FMU in our canonical media storage (S3 in production, filesystem locally).** This remains the source of truth and benefits from S3 encryption, IAM controls, and auditability.
2. **After passing initial validation, copy the FMU into a Modal Volume** (`fmi-cache`) keyed by checksum. Modal functions mount this volume at `/fmus` so subsequent simulations reuse the cached binary without repeated downloads or presigned URLs.
3. **Perform an initial safety/structure check during validator creation/update**:
   - Validate ZIP structure and presence of `modelDescription.xml`.
   - Reject archives containing obviously suspicious binaries or disallowed extensions (e.g., `.exe`, `.dll`, `.dylib`, `.bat`, `*.sh`).
   - Enforce a reasonable size limit and record checksum plus basic metadata.
4. **Security review required** before enabling broad FMU execution:
   - FMU lifecycle: upload, validation, storage in S3, caching in Modal Volume, eviction strategy.
   - Execution isolation: Modal resource limits, network egress, environment variables, volume permissions.
   - Logging/redaction: ensure FMU keys/paths are not logged.
   - Data retention and deletion across S3 and Modal Volumes.

## Consequences

- **Performance:** Modal runner reads from a mounted volume, avoiding per-run downloads; first-run cost is the one-time copy from S3 to Modal.
- **Security posture:** S3 remains the authoritative store; Modal Volume is a constrained cache. Suspicious FMUs are blocked early. A formal review is still required for execution controls and lifecycle policies.
- **Operational:** We introduce a Modal function to accept FMU bytes + checksum and persist into the shared volume. Deploying the updated Modal app is required before enabling run-time use.

## Implementation status (this branch)

- Added FMU checksum + Modal Volume path fields on `FMUModel` and push every validated FMU into a Modal Volume (`/fmus/<checksum>.fmu`) after structural validation (ZIP, modelDescription, disallowed extensions, size cap).
- FMI creation flow now computes checksums, stores the FMU in S3, and writes the same bytes to Modal via `upload_fmu_to_volume`. Metadata (`model_name`, `variable_count`, checksum, volume path) is saved for later runs.
- Modal runner functions (`probe_fmu`, `run_fmi_simulation`) now mount both production and test volumes and resolve FMUs from cache before falling back to URLs. Set `FMI_USE_TEST_VOLUME=1` to isolate test runs.
- Modal EnergyPlus runner also honors `ENERGYPLUS_USE_TEST_VOLUME`/`ENERGYPLUS_TEST_OUTPUT_VOLUME_NAME` to avoid polluting production volumes during integration tests.
- Integration test exercises a real linux64 Feedthrough.fmu on Modal (int_input → int_output) to verify the cache + runner path end to end when credentials are present.
- Documentation updated to clarify the probe concept, S3 + Modal storage flow, and pending security review of FMU lifecycle.

## Action Items

1. Add Modal Volume-backed storage helper in `sv_modal` and expose a function to upload FMUs by checksum.
2. Extend Django FMI creation/update flow to:
   - Validate ZIP structure and block disallowed contents.
   - Compute checksum, store FMU in S3, then push bytes + checksum to Modal Volume.
   - Persist Modal volume key/checksum on `FMUModel` metadata.
3. Update FMI runner to resolve FMUs from the Modal Volume (with a fallback to local path if provided).
4. Document the new flow and call out the pending security review, including FMU lifecycle and eviction policy.
5. Add tests covering validation, checksum propagation, and Modal upload invocation (using fakes in tests).
