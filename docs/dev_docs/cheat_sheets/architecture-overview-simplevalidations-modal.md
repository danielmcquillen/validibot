# Architecture Overview: SimpleValidations × Modal.com

Quick mental model for how we create validators that rely on Modal and how validation runs execute there. Use this as a narrative to remember what lives where and why.

---

## 1) Author creates an FMI validator (and uploads an FMU)

Flow (what you should picture):

1. In the Django UI, the author uploads an FMU while creating an FMI validator.
2. Django immediately sanity-checks the file: it’s a ZIP, has `modelDescription.xml`, no disallowed binaries, and is under size limits.
3. Django computes a checksum and stores the FMU in canonical storage (S3 in production; filesystem locally). S3 is the source of truth.
4. Django then uses the **Modal Python client from the control plane** to copy the FMU into a Modal Volume:
   - Volume: `fmi-cache` (or `fmi-cache-test` when `FMI_USE_TEST_VOLUME=1`).
   - Path inside the volume: `/fmus/<checksum>.fmu` (or `/fmus-test/<checksum>.fmu`).
   - Pattern: `Volume.batch_upload(force=True).put_file(...)` so reruns overwrite the same checksum.
5. Django parses `modelDescription.xml`, seeds catalog entries, and records checksum + volume path on the `FMUModel` so later runs know where to look.
6. Optional probe: Django calls the Modal `probe_fmu` to re-parse variables inside the Modal container; it updates catalog/approval based on that probe.

Outcome: validator references the FMU by checksum; Modal Volume has the FMU; S3 holds the canonical copy.

---

## 2) Running a validation step that uses the validator

Flow (what happens at run time):

1. Workflow run reaches the FMI validation step.
2. Django/FMI engine builds the payload:
   - `fmu_storage_key` (local/S3 path, fallback only)
   - `fmu_checksum` (primary lookup in Modal Volume)
   - `use_test_volume` (true when `FMI_USE_TEST_VOLUME=1`)
   - Inputs, simulation config, desired outputs
3. Django invokes the Modal function `sv_fmi.modal_app.run_fmi_simulation` (Modal client):
   - Credential sources: `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` env vars or `~/.modal.toml` `[default]`.
4. Modal runtime resolves the FMU:
   - Prefer `/fmus[-test]/<checksum>.fmu` from the mounted volume.
   - Optionally fall back to `fmu_url`/`fmu_storage_key` if needed.
5. Modal runs `fmpy.simulate_fmu`, collects requested outputs, returns `FMIRunResult`.
6. Django receives outputs, runs CEL assertions, records results.

---

## Credentials & Volumes

- Put tokens in env or `~/.modal.toml` with `[default] token_id/token_secret`.
- Volumes:
  - Prod: `fmi-cache` mounted at `/fmus`
  - Test: `fmi-cache-test` mounted at `/fmus-test`
  - Create if missing: `modal volume create <name>`
- Test vs prod: use `FMI_USE_TEST_VOLUME=1` to keep local/integration runs from touching prod volumes. Both volumes are mounted; the flag just switches which mount path/checksum is used.

---

## Commands & Debugging

- Deploy FMI app: `modal deploy -m sv_fmi.modal_app`
- Inspect volume: `modal volume ls fmi-cache-test`, `modal volume get fmi-cache-test /fmus-test/<checksum>.fmu`
- Stream logs: `modal logs fmi-runner run_fmi_simulation --tail`
- If a run is stuck “waiting for a container,” redeploy (`modal deploy ...`) and ensure the image builds; check `modal logs` for build or runtime errors.
- If you see `UNAUTHENTICATED`, refresh tokens (`modal token new`), update env or `~/.modal.toml`, and rerun.

---

## ASCII sketch (control-plane upload → Modal run)

```
Author ----upload FMU----> Django
   |                         |
   | checksum, validate      |
   |                         v
   |               [S3/FS canonical copy]
   |                         |
   |---batch_upload(force)--> Modal Volume (/fmus[-test]/<checksum>.fmu)
                            |
Workflow run --------------- |
   | build payload (checksum, inputs, use_test_volume)
   v
Modal function run_fmi_simulation
   | resolve FMU from volume
   | simulate via fmpy
   v
FMIRunResult -> Django -> CEL assertions -> stored run results
```

---

## Why this shape?

- **Control-plane uploads only:** We never spin up a Modal function to upload files; Django uploads to S3 (canonical) and to the Modal Volume in one shot. This is simpler, avoids presigned URLs at run-time, and keeps uploads authenticated on the control plane.
- **Checksum addressing:** The checksum is the stable key across S3 and Modal; it prevents duplicate content and keeps Modal runs deterministic.
- **Prod/test volumes:** Isolation for local/integration work; no accidental writes to prod volumes when running tests.
- **Modal runs are short-lived:** The Modal runner only needs to read the cached FMU and produce outputs; all heavy lifting (upload, catalog seeding) happens in Django.
