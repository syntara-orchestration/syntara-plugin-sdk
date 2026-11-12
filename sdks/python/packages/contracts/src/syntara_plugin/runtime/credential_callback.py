"""Invocation-scoped credential callbacks over the container stream."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from threading import Event, Lock, RLock
from typing import Any, NoReturn
from weakref import finalize

from syntara_plugin.contracts import canonical_json_bytes

from ._toolbox import _ContainerToolbox, _InvocationRequestContext
from .credentials import CredentialAccessError, ScopedCredentialProvider
from .protocol import container_pb2
from .toolbox import _close_toolbox, _new_toolbox

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


class _CredentialScopeState:
    """Scope policy shared with the transport under one lock."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.active = True
        self.allowed: frozenset[str] | None = None


class _CredentialCallbackTransport:
    """Correlate credential messages over one invocation's stream."""

    def __init__(
        self,
        send: Callable[[container_pb2.CredentialRequest], None],
        scope: _CredentialScopeState,
    ) -> None:
        self._send = send
        self._scope = scope
        self._pending: dict[str, tuple[Event, list[container_pb2.CredentialResponse]]] = {}

    @contextmanager
    def request_credential(
        self, name: str, context: _InvocationRequestContext
    ) -> Iterator[container_pb2.CredentialResponse]:
        request_id = uuid.uuid4().hex
        ready = Event()
        responses: list[container_pb2.CredentialResponse] = []
        try:
            self._begin_read(name, context, request_id, ready, responses)
            yield self._await_response(context, ready, responses)
        finally:
            with self._scope.lock:
                self._pending.pop(request_id, None)

    def _begin_read(
        self,
        name: str,
        context: _InvocationRequestContext,
        request_id: str,
        ready: Event,
        responses: list[container_pb2.CredentialResponse],
    ) -> None:
        with self._scope.lock:
            if (
                not self._scope.active
                or context.cancellation.is_set()
                or time.monotonic() >= context.deadline
            ):
                raise CredentialAccessError("expired")
            if self._scope.allowed is None or name not in self._scope.allowed:
                raise CredentialAccessError("missing")
            if len(self._pending) >= _MAX_PENDING:
                raise CredentialAccessError("unavailable")
            self._pending[request_id] = (ready, responses)
            self._send(
                container_pb2.CredentialRequest(
                    invocation_id=context.invocation_id, request_id=request_id, name=name
                )
            )

    def _await_response(
        self,
        context: _InvocationRequestContext,
        ready: Event,
        responses: list[container_pb2.CredentialResponse],
    ) -> container_pb2.CredentialResponse:
        deadline = min(context.deadline, time.monotonic() + _READ_TIMEOUT)
        while not ready.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
            if context.cancellation.is_set() or time.monotonic() >= deadline:
                raise CredentialAccessError("unavailable")
        with self._scope.lock:
            if not self._scope.active or context.cancellation.is_set() or not responses:
                raise CredentialAccessError("expired")
            if time.monotonic() >= deadline:
                raise CredentialAccessError("unavailable")
            return responses[0]

    def receive_credential_response(
        self, response: container_pb2.CredentialResponse, context: _InvocationRequestContext
    ) -> None:
        with self._scope.lock:
            pending = self._pending.get(response.request_id)
            if (
                not self._scope.active
                or response.invocation_id != context.invocation_id
                or pending is None
            ):
                return
            ready, responses = pending
            if not ready.is_set():
                responses.append(response)
                ready.set()

    def close(self) -> None:
        with self._scope.lock:
            self._scope.active = False
            self._scope.allowed = None
            for ready, responses in self._pending.values():
                responses.clear()
                ready.set()
            self._pending.clear()


class CallbackCredentialProvider:
    """One invocation's revocable credential provider."""

    def __init__(
        self,
        invocation_id: str,
        send: Callable[[container_pb2.CredentialRequest], None],
        cancellation: Event,
        timeout: float,
    ) -> None:
        self._context = _InvocationRequestContext(
            invocation_id, cancellation, time.monotonic() + timeout
        )
        self._scope = _CredentialScopeState()
        self._toolbox = _ContainerToolbox(
            self._context, _CredentialCallbackTransport(send, self._scope)
        )
        self._public_toolbox = _new_toolbox(cancellation)

    def restrict(
        self, names: frozenset[str], on_read: Callable[[Mapping[str, Any]], None]
    ) -> ScopedCredentialProvider:
        with self._scope.lock:
            if not self._scope.active or self._scope.allowed is not None:
                raise CredentialAccessError("unauthorized")
            self._scope.allowed = names
        return _new_scoped_callback(self, names, on_read)

    def read(self, name: str) -> dict[str, Any]:
        with self._toolbox._request_credential(name) as response:
            return self._decode_response(response)

    @staticmethod
    def _decode_response(response: container_pb2.CredentialResponse) -> dict[str, Any]:
        if response.WhichOneof("outcome") == "error":
            reason = (
                "missing"
                if response.error == container_pb2.CREDENTIAL_ERROR_MISSING
                else "unavailable"
            )
            raise CredentialAccessError(reason)
        if (
            response.WhichOneof("outcome") != "document_json"
            or len(response.document_json) > _MAX_BYTES
        ):
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
        self._toolbox._receive_credential_response(response)

    def record(
        self, document: Mapping[str, Any], on_read: Callable[[Mapping[str, Any]], None]
    ) -> None:
        with self._scope.lock:
            if (
                not self._scope.active
                or self._context.cancellation.is_set()
                or time.monotonic() >= self._context.deadline
            ):
                raise CredentialAccessError("expired")
            try:
                on_read(document)
            except Exception:
                raise CredentialAccessError("unavailable") from None

    def close(self) -> None:
        self._toolbox._close()
        _close_toolbox(self._public_toolbox)


class _ScopedCallbackState:
    def __init__(
        self,
        provider: CallbackCredentialProvider,
        names: frozenset[str],
        on_read: Callable[[Mapping[str, Any]], None],
    ) -> None:
        self.provider: CallbackCredentialProvider | None = provider
        self.names = names
        self.on_read: Callable[[Mapping[str, Any]], None] | None = on_read
        self.lock = Lock()


# A step can reach the scoped view through its context. Keep the raw callback
# provider off the view's direct attribute path so ordinary view reads record
# their documents. This is not a sandbox for arbitrary in-process introspection.
_callback_states: dict[int, _ScopedCallbackState] = {}
_callback_states_lock = RLock()


def _callback_state(view: _ScopedCallback) -> _ScopedCallbackState:
    with _callback_states_lock:
        return _callback_states[id(view)]


def _discard_callback_state(view_id: int) -> None:
    with _callback_states_lock:
        _callback_states.pop(view_id, None)


def _new_scoped_callback(
    provider: CallbackCredentialProvider,
    names: frozenset[str],
    on_read: Callable[[Mapping[str, Any]], None],
) -> _ScopedCallback:
    view = _ScopedCallback()
    with _callback_states_lock:
        _callback_states[id(view)] = _ScopedCallbackState(provider, names, on_read)
    finalize(view, _discard_callback_state, id(view))
    return view


class _ScopedCallback:
    __slots__ = ("__weakref__",)

    def read(self, name: str) -> Mapping[str, Any]:
        state = _callback_state(self)
        with state.lock:
            provider = state.provider
            if state.on_read is None or provider is None:
                raise CredentialAccessError("expired")
            if name not in state.names:
                raise CredentialAccessError("missing")
        document = provider.read(name)
        with state.lock:
            if state.on_read is None:
                raise CredentialAccessError("expired")
            provider.record(document, state.on_read)
            return document

    def close(self) -> None:
        state = _callback_state(self)
        with state.lock:
            state.on_read = None
            state.names = frozenset()
            if state.provider is not None:
                try:
                    state.provider.close()
                finally:
                    state.provider = None
