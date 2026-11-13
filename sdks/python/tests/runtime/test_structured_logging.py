"""The bounded operational log contract and its terminal delivery."""

from __future__ import annotations

import asyncio
import json
from queue import Queue
from threading import Event, Lock
from typing import Any

import pytest
from syntara_plugin.runtime import (
    ContainerInvocation,
    ContainerInvocationError,
    ContainerResult,
    FailureCode,
)
from syntara_plugin.runtime.container import _run_sync_step, _valid_log_batch
from syntara_plugin.runtime.credential_callback import CallbackCredentialProvider
from syntara_plugin.runtime.protocol import container_pb2
from syntara_plugin.runtime.server import AsyncContainerRuntimeService
from syntara_plugin.runtime.toolbox import _new_toolbox, _take_log_batch


def test_events_are_snapshotted_bounded_and_ordered() -> None:
    toolbox = _new_toolbox(Event(), "invocation")
    attrs = {"count": 1, "nested": ["original"]}
    toolbox.log("INFO", "first", attrs, event_name="item.created")
    attrs["nested"][0] = "changed"
    toolbox.log("WARN", "second")
    for _ in range(128):
        toolbox.log("DEBUG", "extra")
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None
    assert batch.version == 1 and len(batch.events) == 128 and batch.dropped_count == 2
    assert [event.message for event in batch.events[:2]] == ["first", "second"]
    assert json.loads(batch.events[0].attributes_json) == {"count": 1, "nested": ["original"]}
    assert batch.events[0].event_name == "item.created"
    assert _valid_log_batch(batch, "invocation")
    assert batch.ByteSize() <= 262_144
    assert _take_log_batch(toolbox, "invocation") is None
    toolbox.log("INFO", "too late")


@pytest.mark.parametrize(
    "level,message,attributes,name",
    [
        ("info", "message", None, None),
        ("INFO", "", None, None),
        ("INFO", "x" * 4097, None, None),
        ("INFO", "ok", {"bad": float("nan")}, None),
        ("INFO", "ok", {"bad": object()}, None),
        ("INFO", "ok", {"a": 2**63}, None),
        ("INFO", "ok", {"a": "x" * 16385}, None),
        ("INFO", "ok", {}, "bad name"),
        ("INFO", "ok", {str(i): i for i in range(33)}, None),
    ],
)
def test_invalid_logs_never_raise(
    level: str, message: str, attributes: Any, name: str | None
) -> None:
    toolbox = _new_toolbox(Event(), "invocation")
    toolbox.log(level, message, attributes, event_name=name)
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None and batch.dropped_count == 1 and not batch.events


def test_byte_limit_and_cancelled_toolbox() -> None:
    cancellation = Event()
    toolbox = _new_toolbox(cancellation, "invocation")
    for _ in range(30):
        toolbox.log("INFO", "message", {"value": "x" * 16000})
    cancellation.set()
    toolbox.log("ERROR", "ignored")
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None and 0 < len(batch.events) < 30
    assert batch.dropped_count == 30 - len(batch.events)
    assert batch.ByteSize() <= 262_144


@pytest.mark.parametrize("failure", [False, True])
def test_sync_terminal_batch_precedes_result_or_failure(failure: bool) -> None:
    cancellation = Event()
    provider = CallbackCredentialProvider("invocation", lambda _: None, cancellation, 5)
    invocation = ContainerInvocation(
        "invocation",
        "workflow",
        "step",
        {},
        5,
        cancellation=cancellation,
        credential_provider=provider,
    )

    def handler(_: ContainerInvocation) -> ContainerResult:
        provider._public_toolbox.log("INFO", "one")
        provider._public_toolbox.log("INFO", "two")
        if failure:
            raise ContainerInvocationError(
                FailureCode.OUTCOME_UNKNOWN, "failure", outcome="unknown"
            )
        return ContainerResult("sha256:" + "a" * 64, output={})

    outgoing: Queue[Any] = Queue()
    _run_sync_step(handler, invocation, provider, outgoing, Lock())
    batch, terminal, end = outgoing.get(), outgoing.get(), outgoing.get()
    assert batch.WhichOneof("message") == "log_batch"
    assert [event.message for event in batch.log_batch.events] == ["one", "two"]
    assert terminal.event.WhichOneof("event") == ("failure" if failure else "result")
    assert end is None


@pytest.mark.parametrize("failure", [False, True])
def test_async_terminal_batch_precedes_result_or_failure(failure: bool) -> None:
    from syntara_plugin.runtime import StepRegistry

    async def run() -> None:
        cancellation = Event()
        provider = CallbackCredentialProvider("invocation", lambda _: None, cancellation, 5)
        invocation = ContainerInvocation(
            "invocation",
            "workflow",
            "step",
            {},
            5,
            cancellation=cancellation,
            credential_provider=provider,
        )

        def handler(_: ContainerInvocation) -> ContainerResult:
            provider._public_toolbox.log("INFO", "one")
            if failure:
                raise RuntimeError("failure")
            return ContainerResult("sha256:" + "a" * 64, output={})

        service = AsyncContainerRuntimeService(StepRegistry({"step": handler}))
        outgoing: asyncio.Queue[Any] = asyncio.Queue()
        try:
            await service._run_step(handler, invocation, provider, outgoing)
            batch, terminal, end = (
                outgoing.get_nowait(),
                outgoing.get_nowait(),
                outgoing.get_nowait(),
            )
            assert batch.WhichOneof("message") == "log_batch"
            assert terminal.event.WhichOneof("event") == ("failure" if failure else "result")
            assert end is None
        finally:
            service.shutdown()

    asyncio.run(run())


def test_malformed_remote_batch_is_dropped() -> None:
    toolbox = _new_toolbox(Event(), "invocation")
    toolbox.log("INFO", "ok", {"x": 1})
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None
    assert _valid_log_batch(batch, "invocation")
    batch.events[0].attributes_json = b'{"x":1,"x":2}'
    assert not _valid_log_batch(batch, "invocation")
    batch.events[0].attributes_json = b'{"x":NaN}'
    assert not _valid_log_batch(batch, "invocation")


def test_cancel_flushes_existing_logs_and_closes_collector() -> None:
    from syntara_plugin.runtime import StepRegistry

    async def run() -> None:
        cancellation = Event()
        provider = CallbackCredentialProvider("invocation", lambda _: None, cancellation, 5)
        provider._public_toolbox.log("WARN", "before cancellation")
        outgoing: asyncio.Queue[Any] = asyncio.Queue()
        service = AsyncContainerRuntimeService(StepRegistry({"step": lambda _: None}))
        service._register_invocation("invocation", cancellation, provider, outgoing)
        response = await service.Cancel(
            container_pb2.CancelRequest(invocation_id="invocation"), None
        )
        assert response.accepted
        assert outgoing.get_nowait().log_batch.events[0].message == "before cancellation"
        assert outgoing.get_nowait().event.failure.code == "CANCELLED"
        assert outgoing.get_nowait() is None
        provider._public_toolbox.log("INFO", "after cancellation")
        assert _take_log_batch(provider._public_toolbox, "invocation") is None
        service.shutdown()

    asyncio.run(run())


def test_local_diagnostics_render_events_without_changing_them() -> None:
    import io

    from syntara_plugin.sdk.runner import _consume

    toolbox = _new_toolbox(Event(), "invocation")
    toolbox.log("INFO", "created", {"issue": "123"}, event_name="issue.created")
    batch = _take_log_batch(toolbox, "invocation")

    class Client:
        def stream(self, invocation: Any, *, credential_handler: Any, on_log_batch: Any) -> Any:
            on_log_batch(batch)
            yield ContainerResult("sha256:" + "a" * 64, output={})

    stream = io.StringIO()
    invocation = ContainerInvocation("invocation", "workflow", "step", {}, 5)
    _consume(Client(), invocation, None, diagnostic_stream=stream)
    record = json.loads(stream.getvalue())
    assert record["message"] == "created"
    assert record["attributes"] == {"issue": "123"}
    assert record["event_name"] == "issue.created"


def test_language_neutral_batch_fixture() -> None:
    from pathlib import Path

    from google.protobuf.json_format import Parse

    fixture = (
        Path(__file__).resolve().parents[4] / "contracts/fixtures/runtime-log-batch.valid.json"
    )
    batch = Parse(fixture.read_text(), container_pb2.LogBatch())
    assert _valid_log_batch(batch, "invoke-1")
    assert json.loads(batch.events[0].attributes_json) == {"attempts": 1, "issue_id": "123"}
    invalid = fixture.with_name("runtime-log-batch-severity.invalid.json")
    invalid_batch = Parse(invalid.read_text(), container_pb2.LogBatch())
    assert not _valid_log_batch(invalid_batch, "invoke-1")


def test_oversized_nested_attributes_are_rejected_before_container_serialization(
    monkeypatch: Any,
) -> None:
    from syntara_plugin.runtime import toolbox as toolbox_module

    original_dumps = json.dumps
    serialized_containers: list[object] = []

    def record_dumps(value: Any, *args: Any, **kwargs: Any) -> str:
        if isinstance(value, (dict, list)):
            serialized_containers.append(value)
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(toolbox_module.json, "dumps", record_dumps)
    toolbox = _new_toolbox(Event(), "invocation")
    toolbox.log("INFO", "oversized", {"items": ["x" * 16000] * 1000})
    assert serialized_containers == []
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None and batch.dropped_count == 1 and not batch.events


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-32", "utf-32-le"])
def test_remote_attributes_must_be_utf8(encoding: str) -> None:
    toolbox = _new_toolbox(Event(), "invocation")
    toolbox.log("INFO", "ok", {"x": 1})
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None
    batch.events[0].attributes_json = '{"x":1}'.encode(encoding)
    assert not _valid_log_batch(batch, "invocation")


def test_attribute_limit_counts_encoded_json_bytes() -> None:
    toolbox = _new_toolbox(Event(), "invocation")
    toolbox.log("INFO", "at limit", {"v": "x" * 16376})
    toolbox.log("INFO", "over limit", {"v": "x" * 16377})
    toolbox.log("INFO", "utf8", {"v": "é" * 8188})
    toolbox.log("INFO", "escaped", {"v": "\n" * 8188})
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None
    assert batch.dropped_count == 1
    assert [event.message for event in batch.events] == ["at limit", "utf8", "escaped"]
    assert all(len(event.attributes_json) == 16384 for event in batch.events)


def test_mutation_after_validation_does_not_change_serialized_record(monkeypatch: Any) -> None:
    from syntara_plugin.runtime import toolbox as toolbox_module

    attributes = {"items": ["small"]}
    original_dumps = json.dumps
    mutated = False

    def mutate_when_serializing(value: Any, *args: Any, **kwargs: Any) -> str:
        nonlocal mutated
        if isinstance(value, dict) and not mutated:
            mutated = True
            attributes["items"][:] = ["x" * 16000] * 16
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(toolbox_module.json, "dumps", mutate_when_serializing)
    toolbox = _new_toolbox(Event(), "invocation")
    toolbox.log("INFO", "mutation", attributes)
    batch = _take_log_batch(toolbox, "invocation")
    assert mutated
    assert batch is not None and batch.dropped_count == 0
    assert json.loads(batch.events[0].attributes_json) == {"items": ["small"]}
