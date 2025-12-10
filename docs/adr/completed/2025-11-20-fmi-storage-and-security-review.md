# ADR: FMI Storage Strategy and Security Review Workplan

**Status:** Superseded (2025-12-09)  
**Superseded by:** 2025-12-04 Phase 4 FMI Cloud Run (GCS + Cloud Run Jobs)  
**Related ADRs:** 2025-11-17-FMI-Validator, 2025-11-17-FMI-Validator-update

## Context

We now support creating FMI validators that ingest user-uploaded FMUs. The initial implementation fetched FMUs from presigned URLs at run-time and cached them in Modal volumes. That approach has been superseded: Phase 4 migrated FMI execution to Cloud Run Jobs with canonical storage in GCS. This ADR is kept for history; see 2025-12-04 Phase 4 FMI Cloud Run for the current design.

## Decision (superseded by Cloud Run / GCS)

1. **Canonical storage:** FMUs are stored in GCS (filesystem locally) as the source of truth with IAM controls and auditability.
2. **No Modal volume cache:** Modal-specific caching is removed. Cloud Run Jobs pull FMUs directly from GCS using the URI on `FMUModel.gcs_uri`.
3. **Initial safety check during validator creation/update:**
   - Validate ZIP structure and presence of `modelDescription.xml`.
   - Reject archives containing obviously suspicious binaries or disallowed extensions.
   - Enforce size limits and record checksum/metadata.
4. **Security review:** Focus shifts to GCS lifecycle, Cloud Run Job isolation, and callback auth (see Phase 4 ADR). Modal-specific egress/volume concerns no longer apply.

## Consequences

- **Performance:** Cloud Run Jobs stream FMUs from GCS; no Modal volume cache is involved.
- **Security posture:** GCS is authoritative; Cloud Run Jobs run under scoped service accounts with signed callbacks. The Modal-specific review items are obsolete.
- **Operational:** No Modal functions are needed; ensure GCS bucket policies and Cloud Run IAM are configured per Phase 4.

## Implementation status (this branch)

- Added FMU checksum + Modal Volume path fields on `FMUModel` and push every validated FMU into a Modal Volume (`/fmus/<checksum>.fmu`) after structural validation (ZIP, modelDescription, disallowed extensions, size cap).
- FMI creation flow now computes checksums, stores the FMU in S3, and writes the same bytes to Modal via `upload_fmu_to_volume`. Metadata (`model_name`, `variable_count`, checksum, volume path) is saved for later runs.
- Modal runner functions (`probe_fmu`, `run_fmi_simulation`) now mount both production and test volumes and resolve FMUs from cache before falling back to URLs. Set `FMI_USE_TEST_VOLUME=1` to isolate test runs.
- Modal EnergyPlus runner also honors `ENERGYPLUS_USE_TEST_VOLUME`/`ENERGYPLUS_TEST_OUTPUT_VOLUME_NAME` to avoid polluting production volumes during integration tests.
- Integration test exercises a real linux64 Feedthrough.fmu on Modal (int_input → int_output) to verify the cache + runner path end to end when credentials are present.
- Documentation updated to clarify the probe concept, S3 + Modal storage flow, and pending security review of FMU lifecycle.

## Action Items (superseded/completed elsewhere)

Work was completed via the Phase 4 Cloud Run migration:

1. Store FMUs in GCS and persist `gcs_uri` on `FMUModel`.
2. Validate ZIPs, block disallowed contents, compute checksum/metadata.
3. Resolve FMUs from `gcs_uri` in envelope builder/launcher and trigger Cloud Run Jobs.
4. Document the flow and security posture in the Phase 4 ADR.
5. Tests cover checksum propagation, GCS upload, and launch path.
