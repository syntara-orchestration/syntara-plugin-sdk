"""Host-service toolbox for container step implementations."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock
from uuid import UUID, uuid4

from .abi import ContainerInvocationError, FailureCode
from .commands import ContainerCommand, CredentialRefResolutionCommand


class ContainerToolbox:
    """Host services available to a container step implementation."""

    def __init__(
        self,
        invocation_id: str | None = None,
        timeout_seconds: float | None = None,
        emit_command: Callable[[ContainerCommand], None] | None = None,
    ) -> None:
        self._invocation_id = invocation_id
        self._deadline = (
            time.monotonic() + timeout_seconds
            if timeout_seconds is not None
            else None
        )
        self._emit_command = emit_command
        self._pending_commands: dict[str, Future[str]] = {}
        self._commands_lock = Lock()

    def resolve_credential_ref(self, credential_ref: UUID) -> str:
        if not isinstance(credential_ref, UUID):
            raise TypeError("credential_ref must be a UUID")
        if self._emit_command is None or self._invocation_id is None:
            raise ContainerInvocationError(
                FailureCode.ABI_VIOLATION,
                "Credential reference resolution is not available.",
                outcome="not_started",
            )
        remaining = (
            self._deadline - time.monotonic() if self._deadline is not None else None
        )
        if remaining is not None and remaining <= 0:
            raise ContainerInvocationError(
                FailureCode.TIMEOUT,
                "The credential reference resolution exceeded the execution timeout.",
                retryable=True,
                outcome="not_completed",
            )
        command_id = str(uuid4())
        command = CredentialRefResolutionCommand(
            command_id=command_id,
            invocation_id=self._invocation_id,
            credential_ref=credential_ref,
        )
        future: Future[str] = Future()
        with self._commands_lock:
            self._pending_commands[command_id] = future
        try:
            self._emit_command(command)
            return future.result(timeout=remaining)
        except TimeoutError:
            raise ContainerInvocationError(
                FailureCode.TIMEOUT,
                "The credential reference resolution exceeded the execution timeout.",
                retryable=True,
                outcome="not_completed",
            ) from None
        except ContainerInvocationError:
            raise
        except Exception as error:
            raise ContainerInvocationError(
                FailureCode.ABI_VIOLATION,
                "The credential reference could not be resolved.",
                outcome="unknown",
            ) from error
        finally:
            with self._commands_lock:
                self._pending_commands.pop(command_id, None)

    def complete_command(self, command_id: str, value: str) -> bool:
        with self._commands_lock:
            future = self._pending_commands.pop(command_id, None)
        if future is None or future.done():
            return False
        future.set_result(value)
        return True

    def fail_command(
        self, command_id: str, error: ContainerInvocationError
    ) -> bool:
        with self._commands_lock:
            future = self._pending_commands.pop(command_id, None)
        if future is None or future.done():
            return False
        future.set_exception(error)
        return True

    def fail_pending_commands(self, error: ContainerInvocationError) -> None:
        with self._commands_lock:
            futures = list(self._pending_commands.values())
            self._pending_commands.clear()
        for future in futures:
            if not future.done():
                future.set_exception(error)
