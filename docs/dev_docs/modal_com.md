# Modal.com Integration Notes

How we interact with Modal from Validibot and the sibling `sv_modal` project.

## Credentials

- Modal CLI and the Python client use `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` or a config file.
- Preferred config file: `~/.modal.toml` with a `[default]` section:
  ```
  [default]
  token_id = "ak-..."
  token_secret = "as-..."
  ```
- Alternatively, point `MODAL_CONFIG` at a custom path. Environment variables override the file.

## Volumes

- FMI volumes: `fmi-cache` (prod) and `fmi-cache-test` (test). Mounts in Modal at `/fmus` and `/fmus-test`.
- EnergyPlus outputs: `energyplus-outputs` (prod) and `energyplus-outputs-test` (test). Mounts at `/outputs`.
- Create volumes via CLI: `modal volume create <name>`.
- Use test volumes during integration to avoid polluting production: set `FMI_USE_TEST_VOLUME=1` or `ENERGYPLUS_USE_TEST_VOLUME=1`.

## Uploading FMUs (best practice)

- Upload FMUs from the control plane using the Modal Python client; do **not** call a Modal function just to upload.
- Preferred pattern (per Modal docs):
  ```python
  import modal
  volume = modal.Volume.from_name("fmi-cache", create_if_missing=True)
  remote_name = f"/{checksum}.fmu"
  with volume.batch_upload() as batch:
      batch.put_file(local_path, remote_name)  # or batch.put_file(io.BytesIO(fmu_bytes), remote_name)
  ```
  Fallbacks:
  - `put_file(local_path, remote_path)` if `batch_upload` is unavailable.
  - `volume[remote_name] = fmu_bytes` for legacy clients.
- The checksum is used as the filename. The Modal runtime reads `/fmus/<checksum>.fmu` (or `/fmus-test/...` for test volume).
- Remember volume semantics: changes made from the control plane are visible immediately; changes from inside a container require a `commit()` to persist and `reload()` in other containers to see updates (Modal handles background commits, but explicit `reload()` is needed to pick up mid-run writes in long-lived containers).

## Logging and troubleshooting

- Modal captures stdout/stderr from functions. The FMI runner configures `logging.basicConfig(level=logging.INFO)` and emits INFO logs at probe/run entry; add `logger.info(...)` or `print(...)` as needed.
- In the Modal dashboard, open the function call and click “View all logs” to see output. If you see “Waiting for a container,” the run is queued; ensure the app is deployed and the image builds successfully.
- The CLI can stream logs: `modal logs fmi-runner run_fmi_simulation --tail`.
- Common stuck states:
  - `UNAUTHENTICATED`: tokens missing/invalid; refresh via `modal token new` and set env or `~/.modal.toml`.
  - “Waiting for a container”: app not deployed or image build failed; rerun `modal deploy -m sv_fmi.modal_app` and check build logs.
  - Volume issues: ensure `fmi-cache`/`fmi-cache-test` exist and uploads succeed; use `modal volume ls <name>` to inspect and `modal volume put/get/rm` to manage.

## Runner deployment

- Deploy from `sv_modal_dev`:
  - FMI runner: `modal deploy -m sv_fmi.modal_app`
  - EnergyPlus runner: `modal deploy -m sv_energyplus.modal_app`
- Keep `vb_shared` versions in sync across repos; use `update_modal.sh` in `sv_modal_dev` to upgrade `vb_shared`, ensure volumes, and redeploy.

## Tests

- Django integration tests against Modal require credentials and will skip if missing.
- Modal integrity tests live in `sv_modal_dev/tests/test_modal_integrity.py` and are collected by `pytest`; they run only when `RUN_MODAL_TESTS=1` and credentials are present.
- FMI connectivity test uploads the Feedthrough FMU into the test volume using the control-plane upload pattern above, then calls `run_fmi_simulation`.
