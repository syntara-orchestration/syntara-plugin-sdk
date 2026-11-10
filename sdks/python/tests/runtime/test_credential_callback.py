"""Callback correlation, lifecycle, and real gRPC transport."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic
from typing import Any

import grpc
import pytest
import syntara_plugin.runtime.container as runtime_container
from syntara_plugin.runtime import (
    ContainerClient,
    ContainerInvocation,
    ContainerRemoteError,
    ContainerResult,
    CredentialAccessError,
    CredentialRequestContext,
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

    def read(context: CredentialRequestContext) -> dict[str, str]:
        names.append(context.name)
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

    def read(context: CredentialRequestContext) -> dict[str, str]:
        assert context.name == _NAME
        started.set()
        assert context.cancellation.wait(5)
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
    finally:
        channel.close()
        server.stop(0).wait()


def test_client_signals_resolver_when_remote_stream_closes() -> None:
    invocation = _invocation()
    resolver_started = Event()
    call_started = Event()
    stream_finished = Event()
    contexts: list[CredentialRequestContext] = []

    class CompletingCall:
        def __init__(self) -> None:
            self._callbacks: list[Any] = []

        def add_callback(self, callback: Any) -> None:
            self._callbacks.append(callback)

        def __iter__(self):
            yield container_pb2.ExecuteServerMessage(credential_request=
                container_pb2.CredentialRequest(
                    invocation_id=invocation.invocation_id,
                    request_id="request-1",
                    name=_NAME,
                )
            )
            assert stream_finished.wait(2)
            yield container_pb2.ExecuteServerMessage(event=runtime_container.failure_event(
                invocation.invocation_id,
                runtime_container.ContainerInvocationError(
                    FailureCode.CANCELLED, "The step was cancelled.", outcome="not_completed"
                ),
            ))

        def finish(self) -> None:
            stream_finished.set()
            for callback in self._callbacks:
                callback()

        def cancel(self) -> None:
            return

    call = CompletingCall()

    class FakeStub:
        def Execute(self, requests: Any, *, timeout: float) -> CompletingCall:
            assert timeout == invocation.timeout_seconds
            assert next(requests).WhichOneof("message") == "start"
            call_started.set()
            return call

        def Cancel(self, _request: Any, *, timeout: float | None) -> Any:
            return container_pb2.CancelResponse(accepted=True)

    client = ContainerClient.__new__(ContainerClient)
    client._stub = FakeStub()
    client._active_bridges = {}
    client._active_bridges_lock = runtime_container.Lock()

    def read(context: CredentialRequestContext) -> dict[str, str]:
        contexts.append(context)
        resolver_started.set()
        assert context.cancellation.wait(2)
        return {"token": _SECRET}

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(client.execute, invocation, credential_handler=read)
        assert call_started.wait(1)
        assert resolver_started.wait(1)
        assert client.cancel(invocation.invocation_id)
        call.finish()
        with pytest.raises(ContainerRemoteError) as raised:
            result.result(timeout=2)

    assert raised.value.code is FailureCode.CANCELLED
    assert invocation.cancelled
    assert contexts[0].invocation_id == invocation.invocation_id
    assert contexts[0].name == _NAME
    assert contexts[0].cancelled


def test_deadline_signals_resolver_and_bounds_remaining_time() -> None:
    started = Event()
    cancelled = Event()

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        assert invocation.credential_provider is not None
        view = invocation.credential_provider.restrict(frozenset({_NAME}), lambda document: None)
        try:
            with pytest.raises(CredentialAccessError):
                view.read(_NAME)
        finally:
            view.close()
        return ContainerResult(_DIGEST, output={"ok": True})

    def read(context: CredentialRequestContext) -> dict[str, str]:
        assert context.name == _NAME
        assert context.remaining_seconds <= 0.1
        started.set()
        assert context.cancellation.wait(1)
        cancelled.set()
        return {"token": _SECRET}

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, StepRegistry({"example.step": handler}))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    client = ContainerClient(channel)
    invocation = _invocation()
    try:
        with pytest.raises(ContainerRemoteError) as raised:
            client.execute(invocation, timeout_seconds=0.1, credential_handler=read)
        assert raised.value.code is FailureCode.TIMEOUT
        assert started.is_set()
        assert cancelled.is_set()
    finally:
        channel.close()
        server.stop(0).wait()


def test_credential_resolvers_are_scheduled_by_the_caller() -> None:
    from threading import Lock

    release = Event()
    started: list[int] = []
    started_lock = Lock()

    def read(context: CredentialRequestContext) -> dict[str, str]:
        assert context.name == _NAME
        with started_lock:
            started.append(int(context.invocation_id.removeprefix("invocation-")))
        release.wait(2)
        return {"token": _SECRET}

    bridges = [
        runtime_container._CredentialRequestBridge(
            f"invocation-{index}",
            read,
            container_pb2.ExecuteRequest(invocation_id=f"invocation-{index}"),
            Event(),
            monotonic() + 3,
        )
        for index in range(40)
    ]

    def accept(index: int) -> None:
        bridges[index].accept(container_pb2.CredentialRequest(
            invocation_id=f"invocation-{index}", request_id="request-1", name=_NAME,
        ))

    try:
        with ThreadPoolExecutor(max_workers=40) as executor:
            calls = [executor.submit(accept, index) for index in range(len(bridges))]
            deadline = monotonic() + 1
            while len(started) < 40 and monotonic() < deadline:
                Event().wait(0.005)
            assert len(started) == 40
            release.set()
            for call in calls:
                call.result(timeout=2)
    finally:
        release.set()
        for bridge in bridges:
            bridge.close()


def test_repeated_timeouts_bound_blocked_sync_step_workers() -> None:
    from dataclasses import replace
    from threading import Condition, Lock

    release = Event()
    started: list[str] = []
    recovered: list[str] = []
    started_condition = Condition(Lock())
    recovery_release = Event()

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        if invocation.invocation_id.startswith("recovery-"):
            with started_condition:
                recovered.append(invocation.invocation_id)
                started_condition.notify_all()
            recovery_release.wait()
            return ContainerResult(_DIGEST, output={"ok": True})
        with started_condition:
            started.append(invocation.invocation_id)
            started_condition.notify_all()
        release.wait()
        return ContainerResult(_DIGEST, output={"ok": True})

    server = grpc.server(ThreadPoolExecutor(max_workers=20))
    service = add_container_runtime_service(server, StepRegistry({"example.step": handler}))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    client = ContainerClient(channel)
    try:
        for index in range(24):
            invocation = replace(_invocation(), invocation_id=f"invocation-{index}")
            with pytest.raises(ContainerRemoteError) as error:
                client.execute(invocation, timeout_seconds=0.05)
            if index < 16:
                assert error.value.code is FailureCode.TIMEOUT
                expected_count = index + 1
                with started_condition:
                    assert started_condition.wait_for(
                        lambda count=expected_count: len(started) >= count, timeout=1
                    )
            else:
                assert error.value.code is FailureCode.RATE_LIMITED
        assert len(started) == 16

        with service._worker_threads_lock:
            blocked_workers = tuple(service._worker_threads)
        release.set()
        for worker in blocked_workers:
            worker.join(timeout=2)
        assert all(not worker.is_alive() for worker in blocked_workers)

        with ThreadPoolExecutor(max_workers=16) as pool:
            recovery_calls = [
                pool.submit(
                    client.execute,
                    replace(_invocation(), invocation_id=f"recovery-{index}"),
                    timeout_seconds=5,
                )
                for index in range(16)
            ]
            with started_condition:
                all_slots_recovered = started_condition.wait_for(
                    lambda: len(recovered) == 16, timeout=2
                )
            recovery_release.set()
            recovery_results = [call.result(timeout=6) for call in recovery_calls]

        assert all_slots_recovered
        assert len(recovered) == 16
        assert all(result.output == {"ok": True} for result in recovery_results)
    finally:
        release.set()
        recovery_release.set()
        channel.close()
        server.stop(0).wait()


def test_sync_shutdown_prevents_worker_start_after_startup_pause(monkeypatch: Any) -> None:
    invocation = _invocation()
    outgoing: Queue[Any] = Queue()
    service = runtime_container.ContainerRuntimeService(StepRegistry({
        "example.step": lambda _: ContainerResult(_DIGEST, output={"ok": True}),
    }))
    provider = CallbackCredentialProvider(
        invocation.invocation_id, outgoing.put, invocation.cancellation, invocation.timeout_seconds
    )
    terminal_lock = runtime_container.Lock()
    service._register_invocation(
        invocation.invocation_id, invocation.cancellation, provider, outgoing, terminal_lock
    )

    startup_paused = Event()
    resume_startup = Event()
    handler_started = Event()
    release_handler = Event()
    real_thread = runtime_container.Thread

    def make_thread(*args: Any, **kwargs: Any) -> Thread:
        if kwargs.get("name") == "syntara-runtime-reader":
            startup_paused.set()
            assert resume_startup.wait(2)
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(runtime_container, "Thread", make_thread)

    def handler(_: ContainerInvocation) -> ContainerResult:
        handler_started.set()
        release_handler.wait(2)
        return ContainerResult(_DIGEST, output={"ok": True})

    startup_errors: list[BaseException] = []

    def start_workers() -> None:
        try:
            service._start_invocation_workers(
                iter(()), handler, invocation, provider, outgoing, terminal_lock
            )
        except BaseException as error:
            startup_errors.append(error)

    startup = Thread(target=start_workers)
    try:
        startup.start()
        assert startup_paused.wait(1)
        service.shutdown()
        resume_startup.set()
        startup.join(1)

        assert not startup.is_alive()
        assert startup_errors == []
        assert not handler_started.wait(0.1)
        assert service._worker_threads == set()
    finally:
        resume_startup.set()
        release_handler.set()
        startup.join(2)
        service.shutdown()
        provider.close()
        service._unregister_invocation(invocation.invocation_id, invocation.cancellation)


def test_closed_credential_bridge_discards_late_response() -> None:
    started = Event()
    finished = Event()

    def read(context: CredentialRequestContext) -> dict[str, str]:
        assert context.name == _NAME
        started.set()
        assert context.cancellation.wait(1)
        finished.set()
        return {"token": _SECRET}

    bridge = runtime_container._CredentialRequestBridge(
        "invocation-1",
        read,
        container_pb2.ExecuteRequest(invocation_id="invocation-1"),
        Event(),
        monotonic() + 2,
    )
    requests = bridge.requests()
    assert next(requests).WhichOneof("message") == "start"
    worker = Thread(target=bridge.accept, args=(container_pb2.CredentialRequest(
        invocation_id="invocation-1", request_id="request-1", name=_NAME,
    ),))
    worker.start()
    assert started.wait(1)

    bridge.close(cancelled=True)
    assert finished.wait(1)
    worker.join(1)
    assert not worker.is_alive()
    assert list(requests) == []
    with pytest.raises(Empty):
        bridge._outgoing.get_nowait()


def test_credential_response_is_not_yielded_after_bridge_close() -> None:
    checked_active = Event()
    release_active_check = Event()
    active_state = Event()
    active_state.set()

    class PausingActiveEvent:
        pause_next_check = False

        def is_set(self) -> bool:
            active = active_state.is_set()
            if self.pause_next_check:
                self.pause_next_check = False
                checked_active.set()
                assert release_active_check.wait(2)
            return active

        def set(self) -> None:
            active_state.set()

        def clear(self) -> None:
            active_state.clear()

    bridge = runtime_container._CredentialRequestBridge(
        "invocation-1",
        None,
        container_pb2.ExecuteRequest(invocation_id="invocation-1"),
        Event(),
        monotonic() + 2,
    )
    active_event = PausingActiveEvent()
    bridge._active = active_event
    requests = bridge.requests()
    assert next(requests).WhichOneof("message") == "start"
    active_event.pause_next_check = True
    bridge._outgoing.put(container_pb2.ExecuteClientMessage(
        credential_response=container_pb2.CredentialResponse(
            invocation_id="invocation-1",
            request_id="request-1",
            document_json=b'{"token":"callback-secret-sentinel"}',
        )
    ))

    delivered: list[container_pb2.ExecuteClientMessage] = []
    finished = Event()

    def consume_one() -> None:
        try:
            delivered.append(next(requests))
            next(requests)
        except StopIteration:
            pass
        finally:
            finished.set()

    consumer = Thread(target=consume_one)
    consumer.start()
    assert checked_active.wait(1)

    # Before synchronization is added, the request iterator does not hold
    # the bridge lock between the active check and its yield.
    lock_was_free = bridge._lock.acquire(blocking=False)
    if lock_was_free:
        bridge._lock.release()
        bridge.close(cancelled=True)
        release_active_check.set()
        assert finished.wait(1)
        consumer.join(1)
        assert not consumer.is_alive()
        assert delivered == []
        return

    close_finished = Event()

    def close_bridge() -> None:
        bridge.close(cancelled=True)
        close_finished.set()

    closer = Thread(target=close_bridge)
    closer.start()
    release_active_check.set()
    assert finished.wait(1)
    consumer.join(1)
    closer.join(1)
    assert not consumer.is_alive()
    assert not closer.is_alive()
    assert len(delivered) == 1
    assert delivered[0].WhichOneof("message") == "credential_response"
    assert close_finished.is_set()


def test_resolver_context_has_invocation_deadline_and_cancellation() -> None:
    cancellation = Event()
    contexts: list[CredentialRequestContext] = []
    bridge = runtime_container._CredentialRequestBridge(
        "invocation-1",
        lambda context: (contexts.append(context) or {"token": _SECRET}),
        container_pb2.ExecuteRequest(invocation_id="invocation-1"),
        cancellation,
        monotonic() + 5,
    )
    try:
        next(bridge.requests())
        bridge.accept(container_pb2.CredentialRequest(
            invocation_id="invocation-1", request_id="request-1", name=_NAME,
        ))

        assert len(contexts) == 1
        assert contexts[0].invocation_id == "invocation-1"
        assert contexts[0].name == _NAME
        assert contexts[0].cancellation is cancellation
        assert contexts[0].remaining_seconds <= 5
        assert contexts[0].remaining_seconds > 0
        assert contexts[0].cancelled is False
    finally:
        bridge.close(cancelled=True)
    assert contexts[0].cancelled is True


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
                            credential_handler=lambda request, tenant=tenant: {"tenant": tenant})
                for tenant in ("one", "two")
            ]
            assert [result.result(timeout=5).output for result in results] == [
                {"tenant": "one"}, {"tenant": "two"}
            ]
    finally:
        channel.close()
        server.stop(0).wait()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("malformed", [False, True])
def test_request_stream_end_has_a_terminal_event(
    asynchronous: bool, malformed: bool, monkeypatch: Any,
) -> None:
    from syntara_plugin.runtime.container import _request_from_invocation

    provider_closed = Event()
    release = Event()
    original_close = CallbackCredentialProvider.close

    def close(provider: CallbackCredentialProvider) -> None:
        original_close(provider)
        provider_closed.set()

    monkeypatch.setattr(CallbackCredentialProvider, "close", close)

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        release.wait(5)
        return ContainerResult(_DIGEST, output={"ok": True})

    def consume(port: int) -> list[Any]:
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        try:
            start = container_pb2.ExecuteClientMessage(start=_request_from_invocation(_invocation()))
            messages = [start, start] if malformed else [start]
            return list(container_pb2_grpc.ContainerServiceStub(channel).Execute(iter(messages), timeout=3))
        finally:
            channel.close()

    def check(messages: list[Any]) -> None:
        assert len(messages) == 1
        if malformed:
            assert messages[0].event.failure.code == FailureCode.CANCELLED
            assert messages[0].event.failure.outcome == "not_completed"
        else:
            assert messages[0].event.WhichOneof("event") == "result"

    async def run_async() -> None:
        server = grpc.aio.server()
        container_pb2_grpc.add_ContainerServiceServicer_to_server(
            AsyncContainerRuntimeService(StepRegistry({"example.step": handler})), server
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        task = asyncio.create_task(asyncio.to_thread(consume, port))
        try:
            assert await asyncio.to_thread(provider_closed.wait, 2)
            release.set()
            check(await asyncio.wait_for(task, 2))
        finally:
            release.set()
            await server.stop(0)

    if asynchronous:
        asyncio.run(run_async())
    else:
        server = grpc.server(ThreadPoolExecutor(max_workers=2))
        add_container_runtime_service(server, StepRegistry({"example.step": handler}))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                task = pool.submit(consume, port)
                assert provider_closed.wait(2)
                release.set()
                check(task.result(timeout=2))
        finally:
            release.set()
            server.stop(0).wait()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_half_close_unblocks_pending_credential_read(asynchronous: bool) -> None:
    from syntara_plugin.runtime.container import _request_from_invocation

    half_close = Event()

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        assert invocation.credential_provider is not None
        view = invocation.credential_provider.restrict(frozenset({_NAME}), lambda document: None)
        try:
            try:
                view.read(_NAME)
                raise AssertionError("credential read unexpectedly succeeded")
            except CredentialAccessError as error:
                return ContainerResult(_DIGEST, output={"reason": error.reason})
        finally:
            view.close()

    def consume(port: int) -> list[Any]:
        def requests():
            yield container_pb2.ExecuteClientMessage(start=_request_from_invocation(_invocation()))
            assert half_close.wait(3)

        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        messages = []
        try:
            for message in container_pb2_grpc.ContainerServiceStub(channel).Execute(requests(), timeout=4):
                messages.append(message)
                if message.WhichOneof("message") == "credential_request":
                    half_close.set()
            return messages
        finally:
            half_close.set()
            channel.close()

    def check(messages: list[Any]) -> None:
        assert [message.WhichOneof("message") for message in messages] == ["credential_request", "event"]
        assert messages[1].event.WhichOneof("event") == "result"
        assert messages[1].event.result.output_json == b'{"reason":"expired"}'

    async def run_async() -> None:
        server = grpc.aio.server()
        container_pb2_grpc.add_ContainerServiceServicer_to_server(
            AsyncContainerRuntimeService(StepRegistry({"example.step": handler})), server
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            check(await asyncio.to_thread(consume, port))
        finally:
            half_close.set()
            await server.stop(0)

    if asynchronous:
        asyncio.run(run_async())
    else:
        server = grpc.server(ThreadPoolExecutor(max_workers=2))
        add_container_runtime_service(server, StepRegistry({"example.step": handler}))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            check(consume(port))
        finally:
            half_close.set()
            server.stop(0).wait()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_shutdown_grace_delivers_running_invocation_result(asynchronous: bool, monkeypatch: Any) -> None:
    import signal

    from syntara_plugin.runtime import server as runtime_server
    from syntara_plugin.runtime.container import _install_sigterm_shutdown

    started = Event()
    release = Event()

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        started.set()
        release.wait(5)
        return ContainerResult(_DIGEST, output={"ok": True})

    def consume(port: int) -> ContainerResult:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            return ContainerClient(channel).execute(_invocation())

    async def run_async() -> None:
        grpc_server = grpc.aio.server()
        service = AsyncContainerRuntimeService(StepRegistry({"example.step": handler}))
        container_pb2_grpc.add_ContainerServiceServicer_to_server(service, grpc_server)
        port = grpc_server.add_insecure_port("127.0.0.1:0")
        listening = asyncio.Event()
        draining = asyncio.Event()
        signals = []

        class Server:
            async def start(self) -> None:
                await grpc_server.start()
                listening.set()

            async def stop(self, grace: float) -> None:
                draining.set()
                await grpc_server.stop(grace)

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda _sig, callback: signals.append(callback))
        monkeypatch.setattr(runtime_server, "_create_runtime_server", lambda _config: (Server(), service))
        serving = asyncio.create_task(runtime_server.serve_runtime(None))
        await listening.wait()
        result = asyncio.create_task(asyncio.to_thread(consume, port))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            signals[0]()
            await asyncio.wait_for(draining.wait(), 2)
            release.set()
            assert (await asyncio.wait_for(result, 2)).output == {"ok": True}
            await asyncio.wait_for(serving, 2)
        finally:
            release.set()
            if not serving.done():
                serving.cancel()
                await grpc_server.stop(0)

    if asynchronous:
        asyncio.run(run_async())
    else:
        grpc_server = grpc.server(ThreadPoolExecutor(max_workers=2))
        service = add_container_runtime_service(
            grpc_server, StepRegistry({"example.step": handler}), install_sigterm_shutdown=False
        )
        port = grpc_server.add_insecure_port("127.0.0.1:0")
        grpc_server.start()
        signals = []
        monkeypatch.setattr("os.getpid", lambda: 1)
        monkeypatch.setattr(signal, "getsignal", lambda _sig: signal.SIG_DFL)
        monkeypatch.setattr(signal, "signal", lambda _sig, callback: signals.append(callback))
        _install_sigterm_shutdown(grpc_server, 3, service)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(consume, port)
                assert started.wait(2)
                signals[0](signal.SIGTERM, None)
                release.set()
                assert result.result(timeout=2).output == {"ok": True}
        finally:
            release.set()
            grpc_server.stop(0).wait()


def test_malformed_message_observed_before_sync_terminal_commit_wins(monkeypatch: Any) -> None:
    from syntara_plugin.runtime import container as runtime_container

    result_computed = Event()
    release_result = Event()
    malformed_observed = Event()
    release_failure = Event()
    result_queued = Event()
    original_terminal = runtime_container.terminal_execution_event
    original_failure = runtime_container.failure_event

    class RecordingQueue(Queue):
        def put(self, item: Any, *args: Any, **kwargs: Any) -> None:
            super().put(item, *args, **kwargs)
            if item is not None and item.HasField("event") and item.event.WhichOneof("event") == "result":
                result_queued.set()

    def terminal(invocation: ContainerInvocation, result: ContainerResult) -> Any:
        event = original_terminal(invocation, result)
        result_computed.set()
        assert release_result.wait(3)
        return event

    def failure(invocation_id: str, error: Any) -> Any:
        if error.code == FailureCode.CANCELLED:
            malformed_observed.set()
            assert release_failure.wait(3)
        return original_failure(invocation_id, error)

    monkeypatch.setattr(runtime_container, "Queue", RecordingQueue)
    monkeypatch.setattr(runtime_container, "terminal_execution_event", terminal)
    monkeypatch.setattr(runtime_container, "failure_event", failure)
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(
        server,
        StepRegistry({"example.step": lambda _: ContainerResult(_DIGEST, output={"ok": True})}),
        install_sigterm_shutdown=False,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()

    def consume() -> list[Any]:
        def requests():
            start = container_pb2.ExecuteClientMessage(start=runtime_container._request_from_invocation(_invocation()))
            yield start
            assert result_computed.wait(3)
            yield start

        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            return list(container_pb2_grpc.ContainerServiceStub(channel).Execute(requests(), timeout=4))

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(consume)
            try:
                assert malformed_observed.wait(2)
                release_result.set()
                result_queued.wait(0.5)
            finally:
                release_result.set()
                release_failure.set()
            messages = future.result(timeout=2)
            assert len(messages) == 1
            assert messages[0].event.failure.code == FailureCode.CANCELLED
    finally:
        release_result.set()
        release_failure.set()
        server.stop(0).wait()


@pytest.mark.parametrize("action", ["cancel", "shutdown"])
def test_sync_terminal_publication_cannot_interleave_with_cancel_or_shutdown(
    action: str, monkeypatch: Any,
) -> None:
    result_queued = Event()
    release_result = Event()
    failure_queued = Event()
    release_requests = Event()

    class PausingQueue(Queue):
        def put(self, item: Any, *args: Any, **kwargs: Any) -> None:
            super().put(item, *args, **kwargs)
            if item is None or not item.HasField("event"):
                return
            if item.event.WhichOneof("event") == "result":
                result_queued.set()
                assert release_result.wait(3)
            elif item.event.WhichOneof("event") == "failure":
                failure_queued.set()

    class Context:
        def add_callback(self, callback: Any) -> bool:
            return True

        def is_active(self) -> bool:
            return True

    def requests():
        yield container_pb2.ExecuteClientMessage(start=runtime_container._request_from_invocation(_invocation()))
        release_requests.wait(3)

    monkeypatch.setattr(runtime_container, "Queue", PausingQueue)
    service = runtime_container.ContainerRuntimeService(
        StepRegistry({"example.step": lambda _: ContainerResult(_DIGEST, output={"ok": True})})
    )
    stream = service.Execute(requests(), Context())
    try:
        first = next(stream)
        assert first.event.WhichOneof("event") == "result"
        assert result_queued.wait(2)

        def interrupt() -> None:
            if action == "cancel":
                service.Cancel(container_pb2.CancelRequest(invocation_id="invocation-1"), Context())
            else:
                service.shutdown()

        thread = Thread(target=interrupt)
        thread.start()
        try:
            assert not failure_queued.wait(0.5)
        finally:
            release_result.set()
        thread.join(2)
        assert not thread.is_alive()
        assert list(stream) == []
    finally:
        release_result.set()
        release_requests.set()
        stream.close()
        service.shutdown()


@pytest.mark.parametrize("second_action", ["cancel", "shutdown"])
def test_sync_cancel_terminal_publication_cannot_interleave_with_another_cancellation(
    second_action: str, monkeypatch: Any,
) -> None:
    handler_started = Event()
    release_handler = Event()
    release_requests = Event()
    first_failure_queued = Event()
    release_first_failure = Event()
    second_failure_queued = Event()
    failure_count = 0

    class PausingQueue(Queue):
        def put(self, item: Any, *args: Any, **kwargs: Any) -> None:
            nonlocal failure_count
            super().put(item, *args, **kwargs)
            if item is not None and item.HasField("event") and item.event.WhichOneof("event") == "failure":
                failure_count += 1
                if failure_count == 1:
                    first_failure_queued.set()
                    assert release_first_failure.wait(3)
                else:
                    second_failure_queued.set()

    class Context:
        def add_callback(self, callback: Any) -> bool:
            return True

        def is_active(self) -> bool:
            return True

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        handler_started.set()
        release_handler.wait(3)
        return ContainerResult(_DIGEST, output={})

    def requests():
        yield container_pb2.ExecuteClientMessage(start=runtime_container._request_from_invocation(_invocation()))
        release_requests.wait(3)

    monkeypatch.setattr(runtime_container, "Queue", PausingQueue)
    service = runtime_container.ContainerRuntimeService(StepRegistry({"example.step": handler}))
    stream = service.Execute(requests(), Context())
    started = Event()
    first_message: Queue[Any] = Queue()

    def consume_first() -> None:
        started.set()
        first_message.put(next(stream))

    consumer = Thread(target=consume_first)
    first_cancel = Thread(target=lambda: service.Cancel(
        container_pb2.CancelRequest(invocation_id="invocation-1"), Context()
    ))
    second = None
    try:
        consumer.start()
        assert started.wait(2)
        assert handler_started.wait(2)
        first_cancel.start()
        assert first_failure_queued.wait(2)
        assert first_message.get(timeout=2).event.failure.code == FailureCode.CANCELLED
        if second_action == "cancel":
            second = Thread(target=lambda: service.Cancel(
                container_pb2.CancelRequest(invocation_id="invocation-1"), Context()
            ))
        else:
            second = Thread(target=service.shutdown)
        second.start()
        assert not second_failure_queued.wait(0.5)
        release_first_failure.set()
        first_cancel.join(2)
        second.join(2)
        consumer.join(2)
        assert not first_cancel.is_alive()
        assert not second.is_alive()
        assert list(stream) == []
    finally:
        release_first_failure.set()
        release_handler.set()
        release_requests.set()
        first_cancel.join(2) if first_cancel.ident is not None else None
        if second is not None:
            second.join(2)
        consumer.join(2)
        stream.close()
        service.shutdown()
