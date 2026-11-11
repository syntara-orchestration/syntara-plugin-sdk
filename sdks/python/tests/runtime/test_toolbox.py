"""Private Toolbox credential transport delegation."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from queue import Queue
from threading import Event
from time import monotonic
from typing import Any

import pytest
import syntara_plugin.runtime.credential_callback as callback
from syntara_plugin.runtime import CredentialAccessError
from syntara_plugin.runtime._toolbox import _ContainerToolbox, _InvocationRequestContext
from syntara_plugin.runtime.credential_callback import CallbackCredentialProvider
from syntara_plugin.runtime.protocol import container_pb2

_NAME = "example.tracker.token"


def test_toolbox_binds_the_invocation_context_to_credential_transport() -> None:
    cancellation = Event()
    context = _InvocationRequestContext("invocation-1", cancellation, monotonic() + 5)
    response = container_pb2.CredentialResponse(
        invocation_id="invocation-1", request_id="request-1", document_json=b"{}"
    )

    class Transport:
        def __init__(self) -> None:
            self.requests: list[tuple[str, _InvocationRequestContext]] = []
            self.responses: list[
                tuple[container_pb2.CredentialResponse, _InvocationRequestContext]
            ] = []
            self.closed = False

        @contextmanager
        def request_credential(
            self, name: str, request_context: _InvocationRequestContext
        ) -> Iterator[container_pb2.CredentialResponse]:
            self.requests.append((name, request_context))
            yield response

        def receive_credential_response(
            self,
            received: container_pb2.CredentialResponse,
            request_context: _InvocationRequestContext,
        ) -> None:
            self.responses.append((received, request_context))

        def close(self) -> None:
            self.closed = True

    transport = Transport()
    toolbox = _ContainerToolbox(context, transport)
    with toolbox._request_credential(_NAME) as received:
        assert received is response
    toolbox._receive_credential_response(response)
    toolbox._close()

    assert transport.requests == [(_NAME, context)]
    assert transport.responses == [(response, context)]
    assert transport.closed


def test_provider_delegates_declared_name_and_records_response(monkeypatch: Any) -> None:
    calls: list[str] = []
    original_request = _ContainerToolbox._request_credential

    def capture(
        toolbox: _ContainerToolbox, name: str
    ) -> AbstractContextManager[container_pb2.CredentialResponse]:
        calls.append(name)
        return original_request(toolbox, name)

    monkeypatch.setattr(_ContainerToolbox, "_request_credential", capture)
    requests: Queue[container_pb2.CredentialRequest] = Queue()
    recorded: list[Any] = []
    provider = CallbackCredentialProvider("invocation-1", requests.put, Event(), 5)
    view = provider.restrict(frozenset({_NAME}), recorded.append)
    try:
        with pytest.raises(CredentialAccessError):
            view.read("other.recipe")
        assert calls == []
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(view.read, _NAME)
            request = requests.get(timeout=1)
            assert request.name == _NAME
            provider.receive(
                container_pb2.CredentialResponse(
                    invocation_id="invocation-1",
                    request_id=request.request_id,
                    document_json=b'{"token":"test-value"}',
                )
            )
            assert pending.result(timeout=2) == {"token": "test-value"}
        assert calls == [_NAME]
        assert recorded == [{"token": "test-value"}]
    finally:
        view.close()


def test_request_slot_stays_reserved_while_provider_validates_response(monkeypatch: Any) -> None:
    monkeypatch.setattr(callback, "_MAX_PENDING", 1)
    validating = Event()
    release = Event()
    original_decode = CallbackCredentialProvider._decode_response

    def pause_decode(response: container_pb2.CredentialResponse) -> dict[str, Any]:
        validating.set()
        assert release.wait(2)
        return original_decode(response)

    monkeypatch.setattr(CallbackCredentialProvider, "_decode_response", staticmethod(pause_decode))
    requests: Queue[container_pb2.CredentialRequest] = Queue()
    provider = CallbackCredentialProvider("invocation-1", requests.put, Event(), 5)
    view = provider.restrict(frozenset({_NAME}), lambda document: None)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(view.read, _NAME)
            request = requests.get(timeout=1)
            provider.receive(
                container_pb2.CredentialResponse(
                    invocation_id="invocation-1",
                    request_id=request.request_id,
                    document_json=b"{}",
                )
            )
            try:
                assert validating.wait(1)
                with pytest.raises(CredentialAccessError) as error:
                    view.read(_NAME)
                assert error.value.reason == "unavailable"
            finally:
                release.set()
            assert pending.result(timeout=2) == {}
    finally:
        release.set()
        view.close()


def test_scoped_callback_does_not_expose_or_accept_a_raw_provider() -> None:
    provider = CallbackCredentialProvider("invocation-1", lambda request: None, Event(), 5)
    view = provider.restrict(frozenset({_NAME}), lambda document: None)
    try:
        assert not hasattr(view, "_provider")
        with pytest.raises(AttributeError):
            setattr(view, "_provider", provider)  # noqa: B010
    finally:
        view.close()


def test_abandoned_scoped_view_does_not_retain_credential_scope() -> None:
    import gc
    import weakref

    from syntara_plugin.runtime import ExecutionContext

    provider = CallbackCredentialProvider("invocation-1", lambda request: None, Event(), 5)
    scoped = ExecutionContext("invocation-1", credential_provider=provider)._with_credentials((_NAME,))
    assert scoped._credential_scope is not None
    scope_provider = scoped._credential_scope._provider
    assert scope_provider is not None
    view = getattr(scope_provider, "_ScopedCredentialProvider__provider")  # noqa: B009
    reference = weakref.ref(view)
    del view, scope_provider, scoped
    gc.collect()
    try:
        assert reference() is None
    finally:
        provider.close()
