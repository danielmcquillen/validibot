from __future__ import annotations

import hashlib
import os
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from sv_shared.fmi import FMIRunResult
from sv_shared.fmi import FMIRunStatus


class ModalConnectivityTest(TestCase):
    """
    Integration checks against Modal using configured credentials.

    These tests confirm that authentication works, that the FMI runner functions
    exist, and that we can execute a small FMU on Modal using the cached Volume.
    """

    def setUp(self) -> None:
        super().setUp()
        os.environ.setdefault("FMI_USE_TEST_VOLUME", "1")
        os.environ.setdefault("FMI_TEST_VOLUME_NAME", "fmi-cache-test")
        self._ensure_modal_env()
        if not os.getenv("MODAL_TOKEN_ID") or not os.getenv("MODAL_TOKEN_SECRET"):
            self.skipTest(
                "Modal credentials not configured; skipping connectivity test.",
            )
        try:
            import modal  # noqa: F401
        except ImportError as err:  # pragma: no cover - defensive
            self.skipTest(f"Modal package not installed: {err}")

    def _ensure_modal_env(self) -> None:
        """
        Pull Modal credentials from Django settings into the environment so the
        Modal client can authenticate without manual sourcing.
        """

        token_id = getattr(settings, "MODAL_TOKEN_ID", "") or ""
        token_secret = getattr(settings, "MODAL_TOKEN_SECRET", "") or ""
        if token_id and token_secret:
            os.environ.setdefault("MODAL_TOKEN_ID", token_id)
            os.environ.setdefault("MODAL_TOKEN_SECRET", token_secret)

    def test_modal_executes_feedthrough_fmu_from_volume(self) -> None:
        """
        Upload the linux64 Feedthrough FMU into the Modal Volume and execute it.

        The FMU echoes int_in to int_out. We assert that output is 5.
        """

        import modal
        import modal.exception as exc

        # Sanity check credentials are available before calling Modal.
        if not os.getenv("MODAL_TOKEN_ID") or not os.getenv("MODAL_TOKEN_SECRET"):
            self.skipTest(
                "Modal credentials not configured; skipping FMU execution test.",
            )

        assets_root = Path(__file__).resolve().parent / "assets" / "fmu"
        asset = assets_root / "Feedthrough.fmu"
        payload = asset.read_bytes()
        checksum = hashlib.sha256(payload).hexdigest()

        target_volume_name = os.getenv("FMI_TEST_VOLUME_NAME", "fmi-cache-test")
        volume = modal.Volume.from_name(target_volume_name, create_if_missing=True)
        remote_name = f"/{checksum}.fmu"
        if hasattr(volume, "batch_upload"):
            # force=True so reruns overwrite the existing FMU, avoiding FileExistsError
            with volume.batch_upload(force=True) as batch:  # type: ignore[arg-type]
                batch.put_file(str(asset), remote_name)
        elif hasattr(volume, "put_file"):
            volume.put_file(str(asset), remote_name)
        elif hasattr(volume, "__setitem__"):
            volume[remote_name.lstrip("/")] = payload  # type: ignore[index]
        else:  # pragma: no cover - defensive
            self.fail(
                "Modal Volume does not support batch_upload, "
                "put_file, or byte assignment",
            )

        try:
            runner = modal.Function.from_name("fmi-runner", "run_fmi_simulation")
        except exc.NotFoundError as err:
            self.fail(f"Modal FMI run function missing: {err}")
        except exc.AuthError as err:
            self.fail(f"Modal authentication failed: {err}")

        run_kwargs = {
            "fmu_storage_key": str(asset),
            "fmu_url": None,
            "fmu_checksum": checksum,
            "use_test_volume": True,
            "inputs": {"int_in": 5},
            "simulation_config": {
                "start_time": 0.0,
                "stop_time": 1.0,
                "step_size": 0.1,
            },
            "output_variables": ["int_out"],
            "return_logs": False,
        }
        if hasattr(runner, "call"):
            raw_result = runner.call(**run_kwargs)
        elif hasattr(runner, "remote"):
            raw_result = runner.remote(**run_kwargs)
        else:
            raw_result = runner(**run_kwargs)

        result = FMIRunResult.model_validate(raw_result)
        self.assertEqual(result.status, FMIRunStatus.SUCCESS)
        self.assertEqual(result.outputs.get("int_out"), 5.0)
