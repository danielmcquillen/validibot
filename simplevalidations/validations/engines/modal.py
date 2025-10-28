"""
Shared helpers for validator engines that call out to Modal functions.

The :class:`ModalRunnerMixin` bundles the glue code required to look up Modal
functions at runtime, cache the resolved callable, and provide a convenient
hook for tests to inject fakes. Engines that communicate with Modal can inherit
from this mixin alongside :class:`BaseValidatorEngine`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from typing import ClassVar

from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

try:  # pragma: no cover - optional dependency
    from modal import Function as ModalFunction
except Exception:  # pragma: no cover - modal not installed
    ModalFunction = None  # type: ignore[assignment]


_FALSEY_STRINGS = {"0", "false", "False"}


@dataclass(slots=True)
class _ModalRunnerState:
    runner: Callable[..., Any] | None = None
    error: str | None = None


class ModalRunnerMixin:
    """
    Mixin that equips validator engines with Modal lookup and invocation helpers.

    Subclasses must define ``modal_app_name`` and ``modal_function_name``.
    They can override ``modal_return_logs_env_var`` and ``modal_return_logs_default``
    when the downstream Modal function accepts a ``return_logs`` flag.
    """

    modal_app_name: ClassVar[str]
    modal_function_name: ClassVar[str]
    modal_return_logs_env_var: ClassVar[str | None] = None
    modal_return_logs_default: ClassVar[bool] = True

    _modal_runner_state: ClassVar[_ModalRunnerState] = _ModalRunnerState()
    _modal_function_cls: ClassVar[Any] = ModalFunction

    @classmethod
    def configure_modal_runner(cls, mock_callable: Callable[..., Any] | None) -> None:
        """
        Allow tests to inject a fake Modal runner instead of resolving via lookup.
        """

        cls._modal_runner_state = _ModalRunnerState(
            runner=mock_callable,
            error=None,
        )

    @classmethod
    def _get_modal_runner(cls) -> Callable[..., Any]:
        """
        Resolve and cache the Modal function used to execute a validation run.
        """

        if cls._modal_runner_state.runner is not None:
            return cls._modal_runner_state.runner
        if cls._modal_runner_state.error:
            raise RuntimeError(cls._modal_runner_state.error)
        if cls._modal_function_cls is None:
            error_message = _("Install the 'modal' package to run this validation step.")
            cls._modal_runner_state = _ModalRunnerState(
                runner=None,
                error=error_message,
            )
            raise RuntimeError(error_message)
        try:
            runner = cls._modal_function_cls.lookup(
                cls.modal_app_name,
                cls.modal_function_name,
            )
        except Exception as exc:  # pragma: no cover - network or lookup failure
            logger.exception(
                "Modal lookup failed for %s.%s",
                cls.modal_app_name,
                cls.modal_function_name,
            )
            cls._modal_runner_state = _ModalRunnerState(runner=None, error=str(exc))
            raise
        cls._modal_runner_state = _ModalRunnerState(runner=runner, error=None)
        return runner

    @classmethod
    def _should_return_logs(cls) -> bool:
        """
        Determine whether to request logs from the Modal invocation.
        """

        if not cls.modal_return_logs_env_var:
            return cls.modal_return_logs_default
        raw = os.getenv(cls.modal_return_logs_env_var)
        if raw is None:
            return cls.modal_return_logs_default
        return raw not in _FALSEY_STRINGS

    def _invoke_modal_runner(
        self,
        *,
        include_logs: bool | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        """
        Invoke the configured Modal function with the given payload.

        ``include_logs`` controls whether a ``return_logs`` flag is sent. When
        left as ``None`` the mixin chooses a sensible default using
        ``_should_return_logs``.
        """

        runner = self._get_modal_runner()
        call_kwargs = dict(payload)
        if include_logs is None:
            include_logs = self._should_return_logs()
        call_kwargs.setdefault("return_logs", include_logs)

        if hasattr(runner, "call"):
            return runner.call(**call_kwargs)
        if callable(runner):  # pragma: no cover - injected doubles
            return runner(**call_kwargs)
        raise RuntimeError("Resolved Modal runner is not callable.")

