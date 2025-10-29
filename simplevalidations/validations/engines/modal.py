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
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar

from django.utils.translation import gettext as _
from modal import Function as ModalFunction

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


_FALSEY_STRINGS = {"0", "false", "False"}


@dataclass(slots=True)
class _ModalRunnerState:
    runner: Callable[..., Any] | None = None
    cleanup_runner: Callable[..., Any] | None = None
    error: str | None = None
    cleanup_error: str | None = None


class ModalRunnerMixin:
    """
    Mixin that equips validator engines with Modal lookup and invocation helpers.

    Subclasses must define :
        ``modal_app_name``
        ``modal_function_name``

    When the downstream Modal function accepts a ``return_logs`` flag,
    the can override:
        ``modal_return_logs_env_var``
        ``modal_return_logs_default``

    """

    modal_app_name: ClassVar[str]
    modal_function_name: ClassVar[str]
    modal_return_logs_env_var: ClassVar[str | None] = None
    modal_return_logs_default: ClassVar[bool] = True
    modal_cleanup_function_name: ClassVar[str | None] = None

    _modal_runner_state: ClassVar[_ModalRunnerState] = _ModalRunnerState()
    _modal_function_cls: ClassVar[Any] = ModalFunction

    @classmethod
    def configure_modal_runner(
        cls,
        mock_callable: Callable[..., Any] | None,
        *,
        cleanup_callable: Callable[..., Any] | None = None,
    ) -> None:
        """
        Allow tests to inject a fake Modal runner instead of resolving via lookup.
        """

        cls._modal_runner_state = _ModalRunnerState(
            runner=mock_callable,
            cleanup_runner=cleanup_callable,
            error=None,
            cleanup_error=None,
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
            error_message = _(
                "Install the 'modal' package to run this validation step."
            )
            cls._modal_runner_state = _ModalRunnerState(
                runner=None,
                error=error_message,
            )
            raise RuntimeError(error_message)
        try:
            runner = cls._modal_function_cls.from_name(
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
    def _get_modal_cleanup(cls) -> Callable[..., Any]:
        """
        Resolve and cache the Modal cleanup function when defined.
        """

        if not cls.modal_cleanup_function_name:
            raise RuntimeError(
                "modal_cleanup_function_name is not configured for this engine.",
            )

        state = cls._modal_runner_state
        if state.cleanup_runner is not None:
            return state.cleanup_runner
        if state.cleanup_error:
            raise RuntimeError(state.cleanup_error)
        if cls._modal_function_cls is None:
            error_message = _(
                "Install the 'modal' package to run this validation step.",
            )
            cls._modal_runner_state = _ModalRunnerState(
                runner=state.runner,
                cleanup_runner=None,
                error=state.error,
                cleanup_error=error_message,
            )
            raise RuntimeError(error_message)
        try:
            cleanup_runner = cls._modal_function_cls.from_name(
                cls.modal_app_name,
                cls.modal_cleanup_function_name,
            )
        except Exception as exc:  # pragma: no cover - network or lookup failure
            logger.exception(
                "Modal cleanup lookup failed for %s.%s",
                cls.modal_app_name,
                cls.modal_cleanup_function_name,
            )
            cls._modal_runner_state = _ModalRunnerState(
                runner=state.runner,
                cleanup_runner=None,
                error=state.error,
                cleanup_error=str(exc),
            )
            raise
        cls._modal_runner_state = _ModalRunnerState(
            runner=state.runner,
            cleanup_runner=cleanup_runner,
            error=state.error,
            cleanup_error=None,
        )
        return cleanup_runner

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
        if hasattr(runner, "remote"):
            return runner.remote(**call_kwargs)
        if callable(runner):  # pragma: no cover - injected doubles
            return runner(**call_kwargs)
        raise RuntimeError("Resolved Modal runner is not callable.")

    def _invoke_modal_cleanup(self, **payload: Any) -> Any:
        """
        Invoke the configured Modal cleanup function.
        """

        cleanup_runner = self._get_modal_cleanup()
        if hasattr(cleanup_runner, "call"):
            return cleanup_runner.call(**payload)
        if hasattr(cleanup_runner, "remote"):
            return cleanup_runner.remote(**payload)
        if callable(cleanup_runner):  # pragma: no cover - injected doubles
            return cleanup_runner(**payload)
        raise RuntimeError("Resolved Modal cleanup runner is not callable.")
