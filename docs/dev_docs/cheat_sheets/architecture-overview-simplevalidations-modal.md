# Architecture Overview: SimpleValidations × Modal.com

Quick mental model for how we create validators that rely on Modal and how validation runs execute there.

---

## 1) Author creates an FMI validator (and uploads an FMU)

Flow:

1. Author uploads an FMU in the Django app (validator creation screen).
2. Django validates the archive (ZIP, `modelDescription.xml`, disallowed extensions, size cap).
3. Django computes a checksum and stores the FMU in canonical storage (S3 in prod; filesystem locally).
4. Django uses the Modal Python client to write the FMU into the Modal Volume:
   - Volume: `fmi-cache` (or `fmi-cache-test` when `FMI_USE_TEST_VOLUME=1`).
   - Path: `/fmus/<checksum>.fmu` (or `/fmus-test/<checksum>.fmu`).
   - Use `Volume.batch_upload(force=True).put_file(...)`; overwrite if it already exists.
5. Django parses `modelDescription.xml`, seeds catalog entries, and saves checksum + modal volume path on the `FMUModel`.
6. Optional probe: Django calls `probe_fmu` (Modal) to re-parse variables; updates catalog/approval accordingly.

Outcome: validator references the FMU by checksum; Modal Volume has the FMU; S3 holds the canonical copy.

---

## 2) Running a validation step that uses the validator

Flow:

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

---

## Commands & Debugging

- Deploy FMI app: `modal deploy -m sv_fmi.modal_app`
- Inspect volume: `modal volume ls fmi-cache-test`, `modal volume get fmi-cache-test /fmus-test/<checksum>.fmu`
- Stream logs: `modal logs fmi-runner run_fmi_simulation --tail`

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
