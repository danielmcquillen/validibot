# Related Libraries

SimpleValidations collaborates with two sibling projects that live alongside this repository. This page records their roles so future changes keep the three codebases aligned.

## Project Layout

- `../sv_shared`: Shared Python package installed into this project as the `sv-shared` dependency. Source lives in the neighbouring repository, but a vendored version is located inside the virtual environment at `.venv/lib/python3.x/site-packages/sv_shared`. Any changes should be made in sv_shared directly and it's version incremented.
- `../sv_modal`: Django + Modal.com orchestration code that runs remote validation jobs. There is no direct Python path import from this project, so use filesystem-relative imports or API contracts when wiring up engines.

sv_modal installs sv_shared via a github reference. Do not change this. When sv_shared is updated, you must reinstall it i the sv_modal project like:

    uv lock --upgrade-package sv-shared && uv sync --dev.

## sv_shared

`sv_shared` defines payload schemas, enums, and helpers used by both the Django app and Modal workers. When implementing features in `simplevalidations`:

- Inspect the authoritative models (for example, `../sv_shared/energyplus/models.py`) before changing serializers or engine payloads.
- If a contract change is required, update `sv_shared` first, publish a compatible version, and upgrade `requirements/base.txt` (plus regenerate the legacy requirement sets).
- Document any breaking or additive change in both repos—ideally via README updates or release notes—so engineers know which version pairs are compatible.

## sv_modal

`sv_modal` hosts the Modal.com workflow runners. Engines in `simplevalidations` (such as `EnergyPlusEngine`) communicate with this project via API calls, queues, or job triggers defined there.

- Before editing engine code, review the corresponding handler in `../sv_modal` to confirm expected input/output contracts.
- When adding new engine features, implement and test the Modal-side worker in `sv_modal` first, then update `simplevalidations` to call the new behavior.
- Capture any cross-repo assumptions in docstrings or comments so future developers know where to look if behavior changes.

## Working Across Repos

- Keep all three repositories checked out in their respective folders.
- During development, open the relevant modules in `../sv_shared` and `../sv_modal` alongside the Django code to avoid contract drift.
- When touching integrations, note follow-up actions (tests, dependency bumps, deployment sequencing) in the tracking issue or project board.
- Make sure to update documentation in both extenal projects and changes are effected while working in this one.
- When `sv_shared` changes, bump its version, publish/push it, then upgrade both this project and `sv_modal` to that version (do **not** swap `sv_modal` to a local editable reference). In `sv_modal`, run `uv lock --upgrade-package sv-shared && uv sync --dev`.
- When `sv_modal` changes and you need those changes available to tests, redeploy the Modal apps (for example `modal deploy -m sv_fmi.modal_app` and `modal deploy -m sv_energyplus.modal_app`) and ensure required Modal volumes exist. For isolated runs set `FMI_USE_TEST_VOLUME=1` / `FMI_TEST_VOLUME_NAME` (and the analogous EnergyPlus vars) so test runs do not touch production volumes.
- Use `sv_modal/update_modal.sh` as a one-stop script: it upgrades `sv_shared`, syncs deps, creates prod/test Modal volumes for FMI and EnergyPlus, and redeploys both Modal apps so Django integration tests hit current runners.
- Upload FMUs to Modal Volumes from the control plane using the Modal Python client (`Volume.from_name(...).put_file(...)` with the checksum as filename). Avoid calling a Modal function just to upload; the runner functions read from the mounted volume by checksum.
