"""Invocation-scoped credential callbacks over the container stream."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping
from threading import Event, Lock
from typing import Any, NoReturn

from syntara_plugin.contracts import canonical_json_bytes

from .credentials import CredentialAccessError, ScopedCredentialProvider
from .protocol import container_pb2

_MAX_PENDING = 16
_READ_TIMEOUT = 30.0
_MAX_BYTES = 1_048_576


def _invalid_constant(value: str) -> NoReturn:
    raise ValueError("invalid JSON number")


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class CallbackCredentialProvider:
    """One invocation's revocable transport capability."""

    def __init__(
        self,
        invocation_id: str,
        send: Callable[[container_pb2.CredentialRequest], None],
        cancellation: Event,
        timeout: float,
    ) -> None:
        self._invocation_id = invocation_id
        self._send = send
        self._cancellation = cancellation
        self._deadline = time.monotonic() + timeout
        self._lock = Lock()
        self._active = True
        self._allowed: frozenset[str] | None = None
        self._pending: dict[str, tuple[Event, list[container_pb2.CredentialResponse]]] = {}

    def restrict(
        self, names: frozenset[str], on_read: Callable[[Mapping[str, Any]], None]
    ) -> ScopedCredentialProvider:
        with self._lock:
            if not self._active or self._allowed is not None:
                raise CredentialAccessError("unauthorized")
            self._allowed = names
        return _ScopedCallback(self, names, on_read)

    def read(self, name: str) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        ready = Event()
        responses: list[container_pb2.CredentialResponse] = []
        try:
            self._begin_read(name, request_id, ready, responses)
            response = self._await_response(ready, responses)
            return self._decode_response(response)
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _begin_read(
        self, name: str, request_id: str, ready: Event,
        responses: list[container_pb2.CredentialResponse],
    ) -> None:
        with self._lock:
            if not self._active or self._cancellation.is_set() or time.monotonic() >= self._deadline:
                raise CredentialAccessError("expired")
            if self._allowed is None or name not in self._allowed:
                raise CredentialAccessError("missing")
            if len(self._pending) >= _MAX_PENDING:
                raise CredentialAccessError("unavailable")
            self._pending[request_id] = (ready, responses)
            self._send(container_pb2.CredentialRequest(
                invocation_id=self._invocation_id, request_id=request_id, name=name
            ))

    def _await_response(
        self, ready: Event, responses: list[container_pb2.CredentialResponse]
    ) -> container_pb2.CredentialResponse:
        deadline = min(self._deadline, time.monotonic() + _READ_TIMEOUT)
        while not ready.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
            if self._cancellation.is_set() or time.monotonic() >= deadline:
                raise CredentialAccessError("unavailable")
        with self._lock:
            if not self._active or self._cancellation.is_set() or not responses:
                raise CredentialAccessError("expired")
            if time.monotonic() >= deadline:
                raise CredentialAccessError("unavailable")
            return responses[0]

    @staticmethod
    def _decode_response(response: container_pb2.CredentialResponse) -> dict[str, Any]:
        if response.WhichOneof("outcome") == "error":
            reason = "missing" if response.error == container_pb2.CREDENTIAL_ERROR_MISSING else "unavailable"
            raise CredentialAccessError(reason)
        if response.WhichOneof("outcome") != "document_json" or len(response.document_json) > _MAX_BYTES:
            raise CredentialAccessError("unavailable")
        try:
            document = json.loads(
                response.document_json,
                parse_constant=_invalid_constant,
                object_pairs_hook=_unique_members,
            )
            if not isinstance(document, dict):
                raise ValueError
            canonical_json_bytes(document)
        except (TypeError, ValueError, RecursionError):
            raise CredentialAccessError("unavailable") from None
        return document

    def receive(self, response: container_pb2.CredentialResponse) -> None:
        with self._lock:
            pending = self._pending.get(response.request_id)
            if not self._active or response.invocation_id != self._invocation_id or pending is None:
                return
            ready, responses = pending
            if not ready.is_set():
                responses.append(response)
                ready.set()

    def record(self, document: Mapping[str, Any], on_read: Callable[[Mapping[str, Any]], None]) -> None:
        with self._lock:
            if not self._active or self._cancellation.is_set() or time.monotonic() >= self._deadline:
                raise CredentialAccessError("expired")
            try:
                on_read(document)
            except Exception:
                raise CredentialAccessError("unavailable") from None

    def close(self) -> None:
        with self._lock:
            self._active = False
            self._allowed = None
            for ready, responses in self._pending.values():
                responses.clear()
                ready.set()
            self._pending.clear()


class _ScopedCallback:
    def __init__(
        self, provider: CallbackCredentialProvider, names: frozenset[str],
        on_read: Callable[[Mapping[str, Any]], None],
    ) -> None:
        self._provider = provider
        self._names = names
        self._on_read: Callable[[Mapping[str, Any]], None] | None = on_read
        self._lock = Lock()

    def read(self, name: str) -> Mapping[str, Any]:
        with self._lock:
            if self._on_read is None:
                raise CredentialAccessError("expired")
            if name not in self._names:
                raise CredentialAccessError("missing")
        document = self._provider.read(name)
        with self._lock:
            if self._on_read is None:
                raise CredentialAccessError("expired")
            self._provider.record(document, self._on_read)
            return document

    def close(self) -> None:
        with self._lock:
            self._on_read = None
            self._names = frozenset()
        self._provider.close()
