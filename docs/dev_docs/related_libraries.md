# Related Libraries

Validibot collaborates with two sibling projects that live alongside this repository. This page records their roles so future changes keep the three codebases aligned.

## Project Layout

- `../validibot_shared`: Shared Python package installed into this project as the `validibot-shared` dependency. Source lives in the neighbouring repository, but a vendored version is located inside the virtual environment at `.venv/lib/python3.x/site-packages/validibot_shared`. Any changes should be made in validibot_shared directly and it's version incremented.
- `../sv_modal`: Django + Modal.com orchestration code that runs remote validation jobs. There is no direct Python path import from this project, so use filesystem-relative imports or API contracts when wiring up validators.

sv_modal installs validibot_shared via a github reference. Do not change this. When validibot_shared is updated, you must reinstall it i the sv_modal project like:

    uv lock --upgrade-package validibot-shared && uv sync --dev.

## validibot_shared

`validibot_shared` defines payload schemas, enums, and helpers used by both the Django app and Modal workers. When implementing features in `validibot`:

- Inspect the authoritative models (for example, `../validibot_shared/energyplus/models.py`) before changing serializers or validator payloads.
- If a contract change is required, update `validibot_shared` first, publish a compatible version, and run `uv lock --upgrade-package validibot-shared && uv sync` in this project.
- Document any breaking or additive change in both repos—ideally via README updates or release notes—so engineers know which version pairs are compatible.

## sv_modal

`sv_modal` hosts the Modal.com workflow runners. Validators in `validibot` (such as `EnergyPlusValidator`) communicate with this project via API calls, queues, or job triggers defined there.

- Before editing validator code, review the corresponding handler in `../sv_modal` to confirm expected input/output contracts.
- When adding new validator features, implement and test the Modal-side worker in `sv_modal` first, then update `validibot` to call the new behavior.
- Capture any cross-repo assumptions in docstrings or comments so future developers know where to look if behavior changes.

## Working Across Repos

- Keep all three repositories checked out in their respective folders.
- During development, open the relevant modules in `../validibot_shared` and `../sv_modal` alongside the Django code to avoid contract drift.
- When touching integrations, note follow-up actions (tests, dependency bumps, deployment sequencing) in the tracking issue or project board.
- Make sure to update documentation in both extenal projects and changes are effected while working in this one.
- When `validibot_shared` changes, bump its version, publish/push it, then upgrade both this project and `sv_modal` to that version (do **not** swap `sv_modal` to a local editable reference). In `sv_modal`, run `uv lock --upgrade-package validibot-shared && uv sync --dev`.
- When `sv_modal` changes and you need those changes available to tests, redeploy the Modal apps (for example `modal deploy -m sv_fmi.modal_app` and `modal deploy -m sv_energyplus.modal_app`) and ensure required Modal volumes exist. For isolated runs set `FMU_USE_TEST_VOLUME=1` / `FMU_TEST_VOLUME_NAME` (and the analogous EnergyPlus vars) so test runs do not touch production volumes.
- Use `sv_modal/update_modal.sh` as a one-stop script: it upgrades `validibot_shared`, syncs deps, creates prod/test Modal volumes for FMU and EnergyPlus, and redeploys both Modal apps so Django integration tests hit current runners.
- Upload FMUs to Modal Volumes from the control plane using the Modal Python client (`Volume.from_name(...).put_file(...)` with the checksum as filename). Avoid calling a Modal function just to upload; the runner functions read from the mounted volume by checksum.
