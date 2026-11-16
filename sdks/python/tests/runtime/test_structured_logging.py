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
    ContainerLogBatch,
    ContainerResult,
    FailureCode,
)
from syntara_plugin.runtime.container import (
    ContainerClient,
    _container_log_batch,
    _run_sync_step,
    _valid_log_batch,
)
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
    assert batch.version == 1
    assert len(batch.events) == 128
    assert batch.dropped_count == 2
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
    assert batch is not None
    assert batch.dropped_count == 1
    assert not batch.events


def test_byte_limit_and_cancelled_toolbox() -> None:
    cancellation = Event()
    toolbox = _new_toolbox(cancellation, "invocation")
    for _ in range(30):
        toolbox.log("INFO", "message", {"value": "x" * 16000})
    cancellation.set()
    toolbox.log("ERROR", "ignored")
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None
    assert 0 < len(batch.events) < 30
    assert batch.dropped_count == 30 - len(batch.events)
    assert batch.ByteSize() <= 262_144


@pytest.mark.parametrize("failure", [False, True])
def test_sync_terminal_batch_precedes_result_or_failure(failure: bool) -> None:
    cancellation = Event()
    provider = CallbackCredentialProvider("invocation", lambda _: None, cancellation, 5)
    invocation = ContainerInvocation(
        "invocation",
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
    assert batch is not None
    log_batch = _container_log_batch(batch, "invocation")
    assert log_batch is not None

    class Client:
        def stream(self, invocation: Any, *, credential_handler: Any) -> Any:
            yield log_batch
            yield ContainerResult("sha256:" + "a" * 64, output={})

    stream = io.StringIO()
    invocation = ContainerInvocation("invocation", "step", {}, 5)
    _consume(Client(), invocation, None, diagnostic_stream=stream)
    record = json.loads(stream.getvalue())
    assert record["message"] == "created"
    assert record["attributes"] == {"issue": "123"}
    assert record["event_name"] == "issue.created"


def test_client_exposes_one_validated_public_log_batch() -> None:
    toolbox = _new_toolbox(Event(), "invocation")
    toolbox.log("INFO", "created", {"issue": "123"}, event_name="issue.created")
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None

    public_batch = ContainerClient._read_log_batch(batch, "invocation", False, False)

    assert isinstance(public_batch, ContainerLogBatch)
    assert public_batch.invocation_id == "invocation"
    assert public_batch.version == 1
    assert public_batch.records[0].timestamp.endswith("Z")
    assert public_batch.records[0].severity == "INFO"
    assert public_batch.records[0].message == "created"
    assert public_batch.records[0].event_name == "issue.created"
    assert public_batch.records[0].attributes == {"issue": "123"}
    assert public_batch.dropped_count == 0


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
    assert batch is not None
    assert batch.dropped_count == 1
    assert not batch.events


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
    assert batch is not None
    assert batch.dropped_count == 0
    assert json.loads(batch.events[0].attributes_json) == {"items": ["small"]}


def test_local_log_rendering_scrubs_before_json_encoding() -> None:
    import io

    from syntara_plugin.sdk.runner import _render_log_batch

    secret = 'secret\nwith"escapes-1234567890'
    toolbox = _new_toolbox(Event(), "invocation")
    toolbox.log("INFO", secret, {"nested": [secret]})
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None
    public_batch = _container_log_batch(batch, "invocation")
    assert public_batch is not None
    output = io.StringIO()
    _render_log_batch(public_batch, output, (secret,))
    record = json.loads(output.getvalue())
    assert record["message"] == "***"
    assert record["attributes"] == {"nested": ["***"]}


def test_local_consumer_scrubs_structured_values_without_mutating_batch() -> None:
    import io

    from syntara_plugin.sdk.runner import _consume

    secret = 'secret\nwith"escapes-1234567890'
    toolbox = _new_toolbox(Event(), "invocation")
    toolbox.log("INFO", secret, {secret: [secret, 123456789]})
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None
    public_batch = _container_log_batch(batch, "invocation")
    assert public_batch is not None

    class Client:
        def stream(self, invocation: Any, *, credential_handler: Any) -> Any:
            yield public_batch
            yield ContainerResult("sha256:" + "a" * 64, output={})

    output = io.StringIO()
    _consume(
        Client(), ContainerInvocation("invocation", "step", {}, 5), None,
        diagnostic_stream=output, secrets=(secret,), secret_scalars=(123456789,),
    )
    record = json.loads(output.getvalue())
    assert record["message"] == "***"
    assert record["attributes"] == {"***": ["***", "***"]}
    assert public_batch.records[0].message == secret
    assert public_batch.records[0].attributes == {secret: [secret, 123456789]}


@pytest.mark.parametrize("scenario", ["malformed", "duplicate", "out_of_order", "transport"])
def test_grpc_logging_diagnostics_never_expose_batch_or_remote_details(
    scenario: str, caplog: Any, capsys: Any
) -> None:
    import logging
    import traceback
    from concurrent.futures import ThreadPoolExecutor

    import grpc
    from syntara_plugin.runtime import ContainerRemoteError
    from syntara_plugin.runtime.protocol import container_pb2_grpc

    caplog.set_level(logging.DEBUG)
    secret = "log-diagnostic-secret-sentinel"
    toolbox = _new_toolbox(Event(), "invocation")
    toolbox.log("INFO", secret, {"secret": secret})
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None
    duplicate = container_pb2.LogBatch()
    duplicate.CopyFrom(batch)
    duplicate.events[0].message = "second valid batch"
    assert _valid_log_batch(duplicate, "invocation")
    malformed = container_pb2.LogBatch()
    malformed.CopyFrom(batch)
    malformed.events[0].attributes_json = b'{"invalid":"' + secret.encode() + b'"'
    terminal = container_pb2.ExecuteServerMessage(event=container_pb2.ExecutionEvent(
        invocation_id="invocation", result=container_pb2.Result(
            output_schema_digest="sha256:" + "a" * 64, output_json=b"{}"
        )
    ))

    class Service(container_pb2_grpc.ContainerServiceServicer):
        def Execute(self, requests: Any, context: Any) -> Any:
            next(requests)
            if scenario == "malformed":
                yield container_pb2.ExecuteServerMessage(log_batch=malformed)
            elif scenario in {"duplicate", "transport"}:
                yield container_pb2.ExecuteServerMessage(log_batch=batch)
                if scenario == "duplicate":
                    yield container_pb2.ExecuteServerMessage(log_batch=duplicate)
                else:
                    context.abort(grpc.StatusCode.UNAVAILABLE, secret)
            yield terminal
            if scenario == "out_of_order":
                yield container_pb2.ExecuteServerMessage(log_batch=duplicate)

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    container_pb2_grpc.add_ContainerServiceServicer_to_server(Service(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    observed: list[Any] = []
    trace = ""
    try:
        try:
            observed.extend(ContainerClient(channel).stream(
                ContainerInvocation("invocation", "step", {}, 5)
            ))
        except ContainerRemoteError as error:
            assert scenario == "transport"
            trace = "".join(traceback.format_exception(error))
            assert error.__cause__ is None
            assert error.__context__ is None
    finally:
        channel.close()
        server.stop(0).wait()
    records = [event for event in observed if isinstance(event, ContainerLogBatch)]
    assert len(records) == (1 if scenario in {"duplicate", "transport"} else 0)
    if records:
        assert records[0].records[0].message == secret
    assert secret not in trace
    assert secret not in caplog.text
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


@pytest.mark.parametrize("failure", [False, True])
def test_grpc_runtime_delivers_one_bounded_batch_before_terminal(failure: bool) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime

    import grpc
    from syntara_plugin.runtime import StepRegistry, add_container_runtime_service

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        assert invocation.credential_provider is not None
        toolbox = invocation.credential_provider._public_toolbox
        for index in range(130):
            toolbox.log("INFO", f"event-{index}", {"index": index})
        if failure:
            return ContainerResult(
                "sha256:" + "a" * 64, status_code=1, status_message="failed",
                error_result={
                    "category": "business", "code": "BUSINESS_RULE_FAILED",
                    "message": "failed", "retryable": False,
                },
                error_schema_digest="sha256:" + "b" * 64,
            )
        return ContainerResult("sha256:" + "a" * 64, output={})

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    service = add_container_runtime_service(server, StepRegistry({"step": handler}))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    started = datetime.now(UTC)
    try:
        events = list(ContainerClient(channel).stream(
            ContainerInvocation("invocation", "step", {}, 5)
        ))
    finally:
        channel.close()
        server.stop(0).wait()
        service.shutdown()
    finished = datetime.now(UTC)
    assert len(events) == 2
    batch, terminal = events
    assert isinstance(batch, ContainerLogBatch)
    assert isinstance(terminal, ContainerResult)
    assert terminal.status_code == int(failure)
    assert batch.dropped_count == 2
    assert [record.message for record in batch.records] == [f"event-{i}" for i in range(128)]
    timestamps = [datetime.strptime(record.timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=UTC
    ) for record in batch.records]
    assert started.timestamp() - 0.001 <= min(timestamp.timestamp() for timestamp in timestamps)
    assert max(timestamps) <= finished
    assert timestamps == sorted(timestamps)


def test_local_log_preserves_protocol_timestamp_and_severity() -> None:
    import io

    from syntara_plugin.runtime import ContainerLogRecord
    from syntara_plugin.sdk.runner import _render_log_batch

    batch = ContainerLogBatch("invocation", 1, (ContainerLogRecord(
        timestamp="2026-01-02T03:04:05.678Z", severity="INFO",
        message="year 2026", attributes={"year": 2026}, event_name="year.2026",
    ),), 0)
    output = io.StringIO()
    _render_log_batch(batch, output, ("2026", "INFO"), (2026,))
    record = json.loads(output.getvalue())
    assert record == {
        "timestamp": "2026-01-02T03:04:05.678Z", "severity": "INFO",
        "message": "year ***", "attributes": {"year": "***"}, "event_name": "year.***",
    }


def test_declared_sensitive_scalar_scrubbing_does_not_mutate_wire_batch() -> None:
    import io

    from syntara_plugin.sdk.runner import (
        _redacted_input_log_scalars,
        _render_log_batch,
        _secret_values,
        redact_inputs,
    )

    schema = {
        "type": "object",
        "properties": {"api_token": {"type": "integer", "writeOnly": True}},
    }
    inputs = {"api_token": 123}
    scalars: list[Any] = []
    _redacted_input_log_scalars(inputs, redact_inputs(schema, inputs), scalars)
    toolbox = _new_toolbox(Event(), "invocation")
    toolbox.log("INFO", "token 123", {"value": 123}, event_name="value.123")
    batch = _take_log_batch(toolbox, "invocation")
    assert batch is not None
    original = batch.SerializeToString()
    public_batch = _container_log_batch(batch, "invocation")
    assert public_batch is not None
    output = io.StringIO()
    _render_log_batch(public_batch, output, _secret_values(schema, inputs), tuple(scalars))
    record = json.loads(output.getvalue())
    assert record["message"] == "token ***"
    assert record["event_name"] == "value.***"
    assert record["attributes"] == {"value": "***"}
    assert record["severity"] == "INFO"
    assert batch.SerializeToString() == original
    assert public_batch.records[0].message == "token 123"
    assert public_batch.records[0].attributes == {"value": 123}


@pytest.mark.parametrize("value", [True, False])
def test_local_log_keeps_credential_boolean_text_but_scrubs_attribute(value: bool) -> None:
    import io

    from syntara_plugin.runtime import ContainerLogRecord
    from syntara_plugin.sdk.runner import _render_log_batch

    text = str(value)
    batch = ContainerLogBatch("invocation", 1, (ContainerLogRecord(
        timestamp="2026-01-02T03:04:05.678Z", severity="INFO",
        message=f"credential={text}", attributes={"enabled": value},
        event_name=f"credential.{text}",
    ),), 0)
    output = io.StringIO()
    _render_log_batch(batch, output, (), (value,))
    record = json.loads(output.getvalue())
    assert record["message"] == f"credential={text}"
    assert record["event_name"] == f"credential.{text}"
    assert record["attributes"] == {"enabled": "***"}
    assert batch.records[0].message == f"credential={text}"


def test_local_log_drops_attributes_when_scrubbed_keys_collide() -> None:
    import io

    from syntara_plugin.runtime import ContainerLogRecord
    from syntara_plugin.sdk.runner import _render_log_batch

    secret = "sensitive-key"
    batch = ContainerLogBatch("invocation", 1, (ContainerLogRecord(
        timestamp="2026-01-02T03:04:05.678Z", severity="INFO", message="handled",
        attributes={secret: "first", "***": "second"},
    ),), 0)
    output = io.StringIO()
    _render_log_batch(batch, output, (secret,))
    rendered = output.getvalue()
    assert secret not in rendered
    assert json.loads(rendered)["attributes"] == {}
