"""Callback correlation, lifecycle, and real gRPC transport."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event, Thread
from typing import Any

import grpc
import pytest
from syntara_plugin.runtime import (
    ContainerClient,
    ContainerInvocation,
    ContainerRemoteError,
    ContainerResult,
    CredentialAccessError,
    FailureCode,
    ScopedCredentialProvider,
    StepRegistry,
    add_container_runtime_service,
)
from syntara_plugin.runtime.credential_callback import CallbackCredentialProvider
from syntara_plugin.runtime.protocol import container_pb2, container_pb2_grpc
from syntara_plugin.runtime.server import AsyncContainerRuntimeService

_NAME = "example.tracker.token"
_SECRET = "callback-secret-sentinel"
_DIGEST = "sha256:" + "a" * 64


def _invocation() -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invocation-1", workflow_run_id="workflow-1",
        step_revision_id="example.step", inputs={}, timeout_seconds=5,
    )


def _record_read_failure(view: ScopedCredentialProvider, errors: list[str]) -> None:
    try:
        view.read(_NAME)
    except CredentialAccessError as error:
        errors.append(error.reason)


def test_mismatched_and_late_responses_are_discarded(monkeypatch: Any) -> None:
    import syntara_plugin.runtime.credential_callback as callback

    monkeypatch.setattr(callback, "_READ_TIMEOUT", 0.1)
    requests: Queue[Any] = Queue()
    provider = CallbackCredentialProvider("invocation-1", requests.put, Event(), 5)
    recorded: list[Any] = []
    view = provider.restrict(frozenset({_NAME}), recorded.append)
    errors: list[str] = []

    thread = Thread(target=_record_read_failure, args=(view, errors))
    thread.start()
    request = requests.get(timeout=1)
    provider.receive(container_pb2.CredentialResponse(
        invocation_id="other-invocation", request_id=request.request_id,
        document_json=b'{"token":"wrong"}',
    ))
    thread.join(1)
    assert errors == ["unavailable"]
    provider.receive(container_pb2.CredentialResponse(
        invocation_id="invocation-1", request_id=request.request_id,
        document_json=b'{"token":"late"}',
    ))
    assert recorded == []
    view.close()


def test_scope_close_unblocks_pending_read() -> None:
    requests: Queue[Any] = Queue()
    provider = CallbackCredentialProvider("invocation-1", requests.put, Event(), 5)
    view = provider.restrict(frozenset({_NAME}), lambda document: None)
    result: list[str] = []

    thread = Thread(target=_record_read_failure, args=(view, result))
    thread.start()
    requests.get(timeout=1)
    view.close()
    thread.join(1)
    assert result == ["expired"]
    with pytest.raises(CredentialAccessError):
        view.read(_NAME)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_grpc_callback_returns_only_declared_name(asynchronous: bool) -> None:
    names: list[str] = []
    recorded: list[Any] = []

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        assert invocation.credential_provider is not None
        view = invocation.credential_provider.restrict(frozenset({_NAME}), recorded.append)
        try:
            with pytest.raises(CredentialAccessError):
                view.read("other.recipe")
            assert view.read(_NAME) == {"token": _SECRET}
        finally:
            view.close()
        return ContainerResult(_DIGEST, output={"ok": True})

    def read(name: str) -> dict[str, str]:
        names.append(name)
        return {"token": _SECRET}

    if asynchronous:
        async def run_async() -> None:
            server = grpc.aio.server()
            container_pb2_grpc.add_ContainerServiceServicer_to_server(
                AsyncContainerRuntimeService(StepRegistry({"example.step": handler})), server
            )
            port = server.add_insecure_port("127.0.0.1:0")
            await server.start()
            channel = grpc.insecure_channel(f"127.0.0.1:{port}")
            try:
                result = await asyncio.to_thread(ContainerClient(channel).execute, _invocation(), credential_handler=read)
                assert result.output == {"ok": True}
            finally:
                channel.close()
                await server.stop(0)
        asyncio.run(run_async())
    else:
        server = grpc.server(ThreadPoolExecutor(max_workers=2))
        add_container_runtime_service(server, StepRegistry({"example.step": handler}))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        try:
            assert ContainerClient(channel).execute(_invocation(), credential_handler=read).output == {"ok": True}
        finally:
            channel.close()
            server.stop(0).wait()
    assert names == [_NAME]
    assert recorded == [{"token": _SECRET}]


def test_cancel_revokes_pending_read_and_discards_late_host_response() -> None:
    started = Event()
    release = Event()
    finished = Event()

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        assert invocation.credential_provider is not None
        view = invocation.credential_provider.restrict(frozenset({_NAME}), lambda document: None)
        try:
            with pytest.raises(CredentialAccessError):
                view.read(_NAME)
            finished.set()
        finally:
            view.close()
        return ContainerResult(_DIGEST, output={"ok": True})

    def read(name: str) -> dict[str, str]:
        assert name == _NAME
        started.set()
        release.wait(5)
        return {"token": _SECRET}

    server = grpc.server(ThreadPoolExecutor(max_workers=3))
    add_container_runtime_service(server, StepRegistry({"example.step": handler}))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    client = ContainerClient(channel)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(client.execute, _invocation(), credential_handler=read)
            assert started.wait(1)
            assert client.cancel("invocation-1")
            with pytest.raises(ContainerRemoteError) as error:
                result.result(timeout=1)
            assert error.value.code is FailureCode.CANCELLED
            assert finished.wait(1)
            release.set()
    finally:
        release.set()
        channel.close()
        server.stop(0).wait()


@pytest.mark.parametrize("payload", [b'{"key":1,"key":2}', b'{"key":NaN}', b"[]", b"x" * 1_048_577])
def test_invalid_credential_documents_fail_without_recording(payload: bytes) -> None:
    requests: Queue[Any] = Queue()
    provider = CallbackCredentialProvider("invocation-1", requests.put, Event(), 5)
    recorded: list[Any] = []
    view = provider.restrict(frozenset({_NAME}), recorded.append)
    errors: list[str] = []

    thread = Thread(target=_record_read_failure, args=(view, errors))
    thread.start()
    request = requests.get(timeout=1)
    provider.receive(container_pb2.CredentialResponse(
        invocation_id="invocation-1", request_id=request.request_id, document_json=payload,
    ))
    thread.join(1)
    assert errors == ["unavailable"]
    assert recorded == []
    view.close()


def test_pending_reads_are_bounded_and_revoked_together() -> None:
    requests: Queue[Any] = Queue()
    provider = CallbackCredentialProvider("invocation-1", requests.put, Event(), 5)
    view = provider.restrict(frozenset({_NAME}), lambda document: None)
    errors: list[str] = []

    threads = [Thread(target=_record_read_failure, args=(view, errors)) for _ in range(16)]
    for thread in threads:
        thread.start()
    for _ in threads:
        requests.get(timeout=1)
    with pytest.raises(CredentialAccessError) as error:
        view.read(_NAME)
    assert error.value.reason == "unavailable"
    view.close()
    for thread in threads:
        thread.join(1)
    assert errors == ["expired"] * 16


def test_shared_client_uses_attempt_specific_resolvers() -> None:
    from dataclasses import replace
    from threading import Barrier

    barrier = Barrier(2, timeout=3)

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        assert invocation.credential_provider is not None
        view = invocation.credential_provider.restrict(frozenset({_NAME}), lambda document: None)
        try:
            barrier.wait()
            document = view.read(_NAME)
            return ContainerResult(_DIGEST, output={"tenant": document["tenant"]})
        finally:
            view.close()

    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    add_container_runtime_service(server, StepRegistry({"example.step": handler}))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    client = ContainerClient(channel)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                pool.submit(client.execute, replace(_invocation(), invocation_id=f"invocation-{tenant}"),
                            credential_handler=lambda name, tenant=tenant: {"tenant": tenant})
                for tenant in ("one", "two")
            ]
            assert [result.result(timeout=5).output for result in results] == [
                {"tenant": "one"}, {"tenant": "two"}
            ]
    finally:
        channel.close()
        server.stop(0).wait()
