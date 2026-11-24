"""Credential-safe structured logs and best-effort terminal delivery."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from threading import Barrier, Event, Lock
from typing import Any

import pytest
from syntara_plugin.runtime import (
    ContainerInvocation,
    ContainerResult,
    CredentialAccessError,
    ExecutionContext,
    FileCredentialProvider,
    StepRegistry,
)
from syntara_plugin.runtime import container as container_module
from syntara_plugin.runtime.container import _run_sync_step
from syntara_plugin.runtime.credential_callback import CallbackCredentialProvider
from syntara_plugin.runtime.server import AsyncContainerRuntimeService
from syntara_plugin.runtime.toolbox import _close_toolbox, _take_log_batch

_NAME = "example.security.token"
_SECRET = "log-secret-sentinel"


def _context(tmp_path: Path, document: dict[str, Any] | None = None) -> ExecutionContext:
    path = tmp_path / "credential.json"
    path.write_text(json.dumps({"token": _SECRET} if document is None else document))
    return ExecutionContext(
        "invocation",
        credential_provider=FileCredentialProvider(
            {_NAME: str(path)}, local_directory=tmp_path
        ),
    )._with_credentials((_NAME,))


@pytest.mark.parametrize("field", ["message", "name", "key", "nested", "scalar"])
def test_log_secret_fields_and_late_reads_are_suppressed(tmp_path: Path, field: str) -> None:
    context = _context(tmp_path)
    context.toolbox.log("INFO", "safe first")
    message = _SECRET if field == "message" else "unsafe"
    name = _SECRET if field == "name" else None
    attrs: dict[str, Any] = {}
    if field == "key":
        attrs[_SECRET] = "value"
    elif field == "nested":
        attrs["nested"] = [{"value": _SECRET}]
    elif field == "scalar":
        attrs["value"] = _SECRET
    context.toolbox.log("INFO", message, attrs, event_name=name)
    handle = context.credential(_NAME)
    assert handle.read() == {"token": _SECRET}
    context.toolbox.log("INFO", "safe last")
    _close_toolbox(context.toolbox)
    assert context._credential_scope is not None
    context._credential_scope.close()
    batch = _take_log_batch(context.toolbox, "invocation")
    assert batch is not None
    assert _SECRET.encode() not in batch.SerializeToString()
    assert [event.message for event in batch.events] == ["safe first", "safe last"]
    assert batch.dropped_count == 1
    with pytest.raises(CredentialAccessError):
        handle.read()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_log_collection_failure_does_not_prevent_terminal(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool,
) -> None:
    def fail(*args: Any) -> None:
        raise ValueError(_SECRET)

    monkeypatch.setattr(container_module, "_take_log_batch", fail)
    cancellation = Event()
    provider = CallbackCredentialProvider("invocation", lambda _: None, cancellation, 5)
    invocation = ContainerInvocation(
        "invocation", "step", {}, 5,
        cancellation=cancellation, credential_provider=provider,
    )

    def handler(_: ContainerInvocation) -> ContainerResult:
        provider._public_toolbox.log("INFO", "safe")
        return ContainerResult("sha256:" + "a" * 64, output={})

    if asynchronous:
        async def run() -> None:
            service = AsyncContainerRuntimeService(StepRegistry({"step": handler}))
            outgoing: asyncio.Queue[Any] = asyncio.Queue()
            try:
                await service._run_step(handler, invocation, provider, outgoing)
                assert outgoing.get_nowait().event.WhichOneof("event") == "result"
                assert outgoing.get_nowait() is None
                assert outgoing.empty()
            finally:
                service.shutdown()
        asyncio.run(run())
    else:
        outgoing: Queue[Any] = Queue()
        _run_sync_step(handler, invocation, provider, outgoing, Lock())
        assert outgoing.get_nowait().event.WhichOneof("event") == "result"
        assert outgoing.get_nowait() is None
        assert outgoing.empty()


@pytest.mark.parametrize("value", [123456789, True, False, "escaped\nsecret\"value"])
def test_log_scans_typed_values_and_json_escaping(tmp_path: Path, value: Any) -> None:
    context = _context(tmp_path, {"token": value})
    context.credential(_NAME).read()
    context.toolbox.log("INFO", "unsafe", {"nested": [value]})
    _close_toolbox(context.toolbox)
    batch = _take_log_batch(context.toolbox, "invocation")
    assert batch is not None
    assert not batch.events
    assert batch.dropped_count == 1
    assert context._credential_scope is not None
    context._credential_scope.close()


@pytest.mark.parametrize("budget", ["work", "nodes"])
def test_log_scan_budget_is_shared_across_all_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str,
) -> None:
    from syntara_plugin.runtime import credentials

    monkeypatch.setattr(credentials, "_MAX_SCAN_WORK" if budget == "work" else "_MAX_OUTPUT_NODES",
                        110 if budget == "work" else 5)
    context = _context(tmp_path)
    context.credential(_NAME).read()
    for _ in range(3):
        context.toolbox.log("INFO", "x" * 40)
    assert context._credential_scope is not None
    context._credential_scope.close(finalize_logs=lambda: _close_toolbox(context.toolbox))
    batch = _take_log_batch(context.toolbox, "invocation")
    assert batch is not None
    assert len(batch.events) == 1
    assert batch.dropped_count == 2
    assert _take_log_batch(context.toolbox, "invocation") is None


def test_concurrent_logs_and_reads_are_scanned_before_scope_destruction(tmp_path: Path) -> None:
    context = _context(tmp_path)
    barrier = Barrier(2, timeout=3)

    def log() -> None:
        for _ in range(20):
            context.toolbox.log("INFO", _SECRET)
        barrier.wait()

    def read() -> None:
        barrier.wait()
        context.credential(_NAME).read()

    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = [executor.submit(log), executor.submit(read)]
        for task in pending:
            task.result(timeout=5)
    assert context._credential_scope is not None
    scope = context._credential_scope
    scope.close(finalize_logs=lambda: _close_toolbox(context.toolbox))
    batch = _take_log_batch(context.toolbox, "invocation")
    assert batch is not None
    assert not batch.events
    assert batch.dropped_count == 20
    assert not scope._strings
    assert not scope._scalars
    assert not scope._active


@pytest.mark.parametrize("unavailable", ["exception", "collected", "overflow"])
def test_log_scan_uncertainty_drops_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: str,
) -> None:
    import gc

    from syntara_plugin.runtime.credentials import _CredentialScope

    context = _context(tmp_path)
    toolbox = context.toolbox
    context.credential(_NAME).read()
    toolbox.log("INFO", _SECRET)
    if unavailable == "exception":
        def fail(*args: Any) -> bool:
            raise ValueError(_SECRET)
        monkeypatch.setattr(_CredentialScope, "scan_log", fail)
    elif unavailable == "collected":
        del context
        gc.collect()
    else:
        assert context._credential_scope is not None
        context._credential_scope._overflow = True
    _close_toolbox(toolbox)
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None
    assert not batch.events
    assert batch.dropped_count == 1


def test_cyclic_gc_keeps_live_state_and_cleans_abandoned_views() -> None:
    import gc

    from syntara_plugin.runtime import credential_callback, toolbox

    gc.collect()
    live = []
    for index in range(100):
        provider = CallbackCredentialProvider(str(index), lambda _: None, Event(), 5)
        view = provider.restrict(frozenset(), lambda _: None)
        abandoned_toolbox_id = id(provider._public_toolbox)
        abandoned_view_id = id(view)
        cycle: list[Any] = [provider._public_toolbox, view]
        cycle.append(cycle)
        live.append(toolbox._new_toolbox(Event(), str(index)))
        del provider, view, cycle
        gc.collect()
        assert all(toolbox._toolbox_state(item) is not None for item in live)
        assert abandoned_toolbox_id not in toolbox._states
        assert abandoned_view_id not in credential_callback._callback_states
    live_ids = {id(item) for item in live}
    live.clear()
    gc.collect()
    assert live_ids.isdisjoint(toolbox._states)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("after_close", [False, True])
def test_log_finalization_failure_does_not_prevent_terminal(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool, failure: bool, after_close: bool,
) -> None:
    from syntara_plugin.runtime import ContainerInvocationError, FailureCode, credential_callback

    def fail(*args: Any) -> None:
        if after_close:
            _close_toolbox(*args)
        raise ValueError(_SECRET)

    monkeypatch.setattr(credential_callback, "_close_toolbox", fail)
    cancellation = Event()
    provider = CallbackCredentialProvider("invocation", lambda _: None, cancellation, 5)
    view = provider.restrict(frozenset({"any"}), lambda _: None)
    invocation = ContainerInvocation(
        "invocation", "step", {}, 5,
        cancellation=cancellation, credential_provider=provider,
    )

    def handler(_: ContainerInvocation) -> ContainerResult:
        provider._public_toolbox.log("INFO", "queued")
        if failure:
            raise ContainerInvocationError(FailureCode.REMOTE_REJECTED, "safe failure")
        return ContainerResult("sha256:" + "a" * 64, output={})

    if asynchronous:
        async def run() -> None:
            service = AsyncContainerRuntimeService(StepRegistry({"step": handler}))
            outgoing: asyncio.Queue[Any] = asyncio.Queue()
            try:
                await service._run_step(handler, invocation, provider, outgoing)
                batch = outgoing.get_nowait().log_batch
                assert not batch.events
                assert batch.dropped_count == 1
                event = outgoing.get_nowait().event
                assert event.WhichOneof("event") == ("failure" if failure else "result")
                assert _SECRET.encode() not in event.SerializeToString()
                assert outgoing.get_nowait() is None
            finally:
                service.shutdown()
        asyncio.run(run())
    else:
        outgoing: Queue[Any] = Queue()
        _run_sync_step(handler, invocation, provider, outgoing, Lock())
        batch = outgoing.get_nowait().log_batch
        assert not batch.events
        assert batch.dropped_count == 1
        event = outgoing.get_nowait().event
        assert event.WhichOneof("event") == ("failure" if failure else "result")
        assert _SECRET.encode() not in event.SerializeToString()
        assert outgoing.get_nowait() is None
    with pytest.raises(CredentialAccessError, match="expired"):
        view.read("any")


def test_log_severity_is_screened(tmp_path: Path) -> None:
    context = _context(tmp_path, {"token": "INFO"})
    context.credential(_NAME).read()
    context.toolbox.log("INFO", "safe message")
    _close_toolbox(context.toolbox)
    batch = _take_log_batch(context.toolbox, "invocation")
    assert batch is not None
    assert not batch.events
    assert batch.dropped_count == 1
    assert context._credential_scope is not None
    context._credential_scope.close()


def test_async_cancel_does_not_wait_for_an_in_progress_log_scan() -> None:
    import time
    from threading import Thread, Timer

    from syntara_plugin.runtime.protocol import container_pb2
    from syntara_plugin.runtime.toolbox import _set_log_scanner

    async def run() -> None:
        entered, release = Event(), Event()
        cancellation = Event()
        provider = CallbackCredentialProvider("invocation", lambda _: None, cancellation, 5)
        provider._public_toolbox.log("INFO", "queued")

        def slow_scan(_: Any) -> bool:
            entered.set()
            release.wait(2)
            return True

        _set_log_scanner(provider._public_toolbox, slow_scan)
        worker = Thread(target=_close_toolbox, args=(provider._public_toolbox,))
        worker.start()
        assert entered.wait(1)
        service = AsyncContainerRuntimeService(
            StepRegistry({"step": lambda _: ContainerResult("sha256:" + "a" * 64, output={})})
        )
        outgoing: asyncio.Queue[Any] = asyncio.Queue()
        service._register_invocation("invocation", cancellation, provider, outgoing)
        timer = Timer(0.7, release.set)
        timer.start()
        start = time.monotonic()
        try:
            response = await service.Cancel(container_pb2.CancelRequest(invocation_id="invocation"), None)  # type: ignore[arg-type]
            assert time.monotonic() - start < 0.4
            assert response.accepted
            batch = outgoing.get_nowait().log_batch
            assert not batch.events
            assert batch.dropped_count == 1
            assert outgoing.get_nowait().event.WhichOneof("event") == "failure"
            release.set()
            worker.join(2)
            assert not worker.is_alive()
            assert _take_log_batch(provider._public_toolbox, "invocation") is None
            from syntara_plugin.runtime.toolbox import _toolbox_state
            state = _toolbox_state(provider._public_toolbox)
            assert not state.events
            assert state.dropped == 1
        finally:
            release.set()
            worker.join(2)
            timer.cancel()
            service.shutdown()

    asyncio.run(run())


def test_async_success_scans_off_loop_and_preserves_safe_records() -> None:
    from syntara_plugin.runtime.toolbox import _set_log_scanner

    async def run() -> None:
        entered, release = Event(), Event()
        cancellation = Event()
        provider = CallbackCredentialProvider("invocation", lambda _: None, cancellation, 5)
        invocation = ContainerInvocation(
            "invocation", "step", {}, 5,
            cancellation=cancellation, credential_provider=provider,
        )

        def scan(_: Any) -> bool:
            entered.set()
            return release.wait(2)

        def handler(_: ContainerInvocation) -> ContainerResult:
            provider._public_toolbox.log("INFO", "safe")
            return ContainerResult("sha256:" + "a" * 64, output={})

        _set_log_scanner(provider._public_toolbox, scan)
        service = AsyncContainerRuntimeService(StepRegistry({"step": handler}))
        outgoing: asyncio.Queue[Any] = asyncio.Queue()
        task = asyncio.create_task(service._run_step(handler, invocation, provider, outgoing))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            release.set()
            await asyncio.wait_for(task, 1)
            batch = outgoing.get_nowait().log_batch
            assert [event.message for event in batch.events] == ["safe"]
            assert batch.dropped_count == 0
            assert outgoing.get_nowait().event.WhichOneof("event") == "result"
            assert outgoing.get_nowait() is None
        finally:
            release.set()
            service.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("bound", [False, True])
def test_async_cancel_preserves_already_finalized_logs(bound: bool) -> None:
    from syntara_plugin.runtime.protocol import container_pb2
    from syntara_plugin.runtime.toolbox import _set_log_scanner

    async def run() -> None:
        cancellation = Event()
        provider = CallbackCredentialProvider("invocation", lambda _: None, cancellation, 5)
        provider._public_toolbox.log("INFO", "safe")
        if bound:
            _set_log_scanner(provider._public_toolbox, lambda _: True)
        _close_toolbox(provider._public_toolbox)
        service = AsyncContainerRuntimeService(
            StepRegistry({"step": lambda _: ContainerResult("sha256:" + "a" * 64, output={})})
        )
        outgoing: asyncio.Queue[Any] = asyncio.Queue()
        service._register_invocation("invocation", cancellation, provider, outgoing)
        try:
            response = await service.Cancel(
                container_pb2.CancelRequest(invocation_id="invocation"), None  # type: ignore[arg-type]
            )
            assert response.accepted
            batch = outgoing.get_nowait().log_batch
            assert [event.message for event in batch.events] == ["safe"]
            assert batch.dropped_count == 0
            assert outgoing.get_nowait().event.WhichOneof("event") == "failure"
        finally:
            service.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [False, True])
def test_async_log_scan_cannot_turn_prepared_outcome_into_timeout(failure: bool) -> None:
    from syntara_plugin.runtime import ContainerInvocationError, FailureCode
    from syntara_plugin.runtime.toolbox import _set_log_scanner

    async def run() -> None:
        release = Event()
        cancellation = Event()
        provider = CallbackCredentialProvider("invocation", lambda _: None, cancellation, 5)
        invocation = ContainerInvocation(
            "invocation", "step", {}, 0.05,
            cancellation=cancellation, credential_provider=provider,
        )

        def scan(_: Any) -> bool:
            release.wait(1)
            return True

        def handler(_: ContainerInvocation) -> ContainerResult:
            provider._public_toolbox.log("INFO", "safe")
            if failure:
                raise ContainerInvocationError(FailureCode.REMOTE_REJECTED, "safe failure")
            return ContainerResult("sha256:" + "a" * 64, output={})

        _set_log_scanner(provider._public_toolbox, scan)
        service = AsyncContainerRuntimeService(StepRegistry({"step": handler}))
        outgoing: asyncio.Queue[Any] = asyncio.Queue()
        try:
            await asyncio.wait_for(service._run_step(handler, invocation, provider, outgoing), 0.5)
            batch = outgoing.get_nowait().log_batch
            assert not batch.events
            assert batch.dropped_count == 1
            event = outgoing.get_nowait().event
            assert event.WhichOneof("event") == ("failure" if failure else "result")
            if failure:
                assert event.failure.code == "REMOTE_REJECTED"
        finally:
            release.set()
            service.shutdown()

    asyncio.run(run())


def test_half_close_after_read_keeps_fingerprints_for_queued_logs() -> None:
    from threading import Thread

    from syntara_plugin.runtime.protocol import container_pb2

    requests: Queue[Any] = Queue()
    cancellation = Event()
    provider = CallbackCredentialProvider("invocation", requests.put, cancellation, 5)
    context = ExecutionContext(
        "invocation", cancellation=cancellation, credential_provider=provider,
    )._with_credentials((_NAME,))

    def respond() -> None:
        request = requests.get(timeout=2)
        provider.receive(container_pb2.CredentialResponse(
            invocation_id="invocation", request_id=request.request_id,
            document_json=json.dumps({"token": _SECRET}).encode(),
        ))

    thread = Thread(target=respond)
    thread.start()
    try:
        handle = context.credential(_NAME)
        assert handle.read() == {"token": _SECRET}
        provider.close_credentials()
        context.toolbox.log("INFO", _SECRET)
        assert context._credential_scope is not None
        context._credential_scope.close(finalize_logs=lambda: _close_toolbox(context.toolbox))
        batch = _take_log_batch(context.toolbox, "invocation")
        assert batch is not None
        assert not batch.events
        assert batch.dropped_count == 1
        with pytest.raises(CredentialAccessError, match="expired"):
            handle.read()
    finally:
        thread.join(2)


@pytest.mark.parametrize("failure", [False, True])
def test_sync_log_scan_cannot_turn_prepared_outcome_into_timeout(failure: bool) -> None:
    from threading import Timer

    from syntara_plugin.runtime import ContainerInvocationError, FailureCode
    from syntara_plugin.runtime.container import ContainerRuntimeService, _request_from_invocation
    from syntara_plugin.runtime.protocol import container_pb2
    from syntara_plugin.runtime.toolbox import _set_log_scanner

    release = Event()

    def scan(_: Any) -> bool:
        release.wait(2)
        return True

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        provider = invocation.credential_provider
        assert isinstance(provider, CallbackCredentialProvider)
        provider._public_toolbox.log("INFO", "safe")
        _set_log_scanner(provider._public_toolbox, scan)
        if failure:
            raise ContainerInvocationError(FailureCode.REMOTE_REJECTED, "safe failure")
        return ContainerResult("sha256:" + "a" * 64, output={})

    class Context:
        def add_callback(self, callback: Any) -> bool:
            return True

        def is_active(self) -> bool:
            return True

    invocation = ContainerInvocation("invocation", "step", {}, 1)
    request = container_pb2.ExecuteClientMessage(start=_request_from_invocation(invocation))
    service = ContainerRuntimeService(StepRegistry({"step": handler}))
    timer = Timer(1.5, release.set)
    timer.start()
    try:
        messages = list(service.Execute(iter([request]), Context()))  # type: ignore[arg-type]
        batch = next(message.log_batch for message in messages if message.HasField("log_batch"))
        assert not batch.events
        assert batch.dropped_count == 1
        event = next(message.event for message in messages if message.HasField("event"))
        assert event.WhichOneof("event") == ("failure" if failure else "result")
        if failure:
            assert event.failure.code == "REMOTE_REJECTED"
    finally:
        release.set()
        timer.cancel()
        service.shutdown()


def test_detached_fingerprints_invalidate_the_old_scanner(tmp_path: Path) -> None:
    from syntara_plugin.runtime.credentials import _LogFingerprintScanner

    context = _context(tmp_path)
    context.credential(_NAME).read()
    assert context._credential_scope is not None
    scope = context._credential_scope
    with scope._record_lock:
        scanner = _LogFingerprintScanner(scope)
    assert not scope.scan_log("otherwise safe")
    assert not scanner.scan_log(_SECRET)
    assert scanner.scan_log("otherwise safe")
    scope.close()
