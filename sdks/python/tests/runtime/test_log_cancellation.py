"""Cancellation, timeout and shutdown screen Toolbox logs and keep prepared outcomes.

Each scenario runs over real gRPC against both runtime implementations with a
typed step bound through the SDK adapter, so the log collector carries the
same credential scanner a production step has.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Thread
from typing import Any, ClassVar

import grpc
import pytest
import syntara_plugin.runtime.credentials as credentials_module
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ActionStep,
    ContainerClient,
    ContainerInvocation,
    ContainerInvocationError,
    ContainerLogBatch,
    ContainerRemoteError,
    ContainerResult,
    CredentialAccessError,
    ExecutionContext,
    FailureCode,
    FileCredentialProvider,
    InstalledStepManifest,
    StepBinding,
    StepDescriptor,
    StepRegistry,
    add_container_runtime_service,
    container_step_for_binding,
)
from syntara_plugin.runtime.container import _request_from_invocation
from syntara_plugin.runtime.credentials import _LogFingerprintScanner
from syntara_plugin.runtime.protocol import container_pb2, container_pb2_grpc
from syntara_plugin.runtime.server import AsyncContainerRuntimeService

_SECRET = "cancel-log-secret-sentinel"
_NAME = "acme.cancel.token"
_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


class ProbeInput(BaseModel):
    mode: str


class ProbeOutput(BaseModel):
    result: str


class ProbeStep(ActionStep[ProbeInput, ProbeOutput]):
    started: ClassVar[Event] = Event()
    release: ClassVar[Event] = Event()
    observed: ClassVar[list[str]] = []

    def run(self, inputs: ProbeInput, context: ExecutionContext) -> ProbeOutput:
        mode = inputs.mode
        context.toolbox.log("INFO", "safe before")
        if mode == "inflight_read":
            # The record already holds the value; the read is cut off mid-flight.
            context.toolbox.log("INFO", f"token {_SECRET}")
            ProbeStep.started.set()
            try:
                context.credential(_NAME).read()
                ProbeStep.observed.append("read")
            except CredentialAccessError as error:
                ProbeStep.observed.append(str(error))
            # Keep running so the termination path, not the step, commits.
            context.cancellation.wait(5)
            return ProbeOutput(result="done")
        if mode.startswith("read"):
            # Queued before the read: must still be screened against it.
            context.toolbox.log("INFO", f"token {_SECRET}")
            context.credential(_NAME).read()
            context.toolbox.log("INFO", "safe after")
        ProbeStep.started.set()
        if mode.endswith("cooperative"):
            context.cancellation.wait(5)
        elif mode.endswith("blocking"):
            ProbeStep.release.wait(5)
        if mode == "wait_then_read_cooperative":
            try:
                context.credential(_NAME).read()
                ProbeStep.observed.append("read")
            except CredentialAccessError as error:
                ProbeStep.observed.append(str(error))
        if mode == "fail":
            raise ContainerInvocationError(FailureCode.REMOTE_REJECTED, "safe failure")
        return ProbeOutput(result="done")


def _binding() -> StepBinding:
    metadata = {"name": "probe", "displayName": "Probe", "description": "Probe."}
    targets = [
        {"apiVersion": "syntara.io/v1alpha1", "kind": "IntegrationType", "metadata": metadata,
         "spec": {"id": "acme.cancel.integration", "endpointKinds": ["https"],
                  "credentialTypes": [_NAME], "configuration": {"kind": "inline", "value": {}}}},
        {"apiVersion": "syntara.io/v1alpha1", "kind": "CredentialRecipe", "metadata": metadata,
         "spec": {"id": _NAME, "integrationType": "acme.cancel.integration",
                  "credentialSchema": {"kind": "inline",
                                       "value": {"$schema": _SCHEMA, "type": "object"}},
                  "secretFields": ["token"], "issuance": {"kind": "static-bearer.v1"}}},
    ]
    manifest = {
        "apiVersion": "syntara.io/v1alpha1", "kind": "Action",
        "metadata": {"name": "probe", "displayName": "Probe", "description": "Probe logs."},
        "spec": {
            "runtime": {"kind": "custom-runner", "entryPoint": f"{__name__}:ProbeStep"},
            "integrationType": "acme.cancel.integration",
            "input": {"kind": "inline",
                      "value": {"$schema": _SCHEMA, **ProbeInput.model_json_schema()}},
            "output": {"kind": "inline",
                       "value": {"$schema": _SCHEMA, **ProbeOutput.model_json_schema()}},
        },
    }
    installed = InstalledStepManifest.from_manifest(
        "acme.cancel.probe", manifest, {}, credential_targets=targets
    )
    step = ProbeStep()
    return StepBinding(StepDescriptor.for_installed_step(step, installed), step)


_BINDING = _binding()
_STEP = _BINDING.descriptor.step_identity


@pytest.fixture(autouse=True)
def _reset_probe() -> Iterator[None]:
    ProbeStep.started = Event()
    ProbeStep.release = Event()
    ProbeStep.observed = []
    yield
    ProbeStep.release.set()


@pytest.fixture
def slow_scan(monkeypatch: pytest.MonkeyPatch) -> tuple[Event, Event]:
    """Block every detached-fingerprint scan until released."""
    entered, release = Event(), Event()
    original = _LogFingerprintScanner.scan_log

    def scan_log(scanner: _LogFingerprintScanner, value: Any) -> bool:
        entered.set()
        release.wait(3)
        return original(scanner, value)

    monkeypatch.setattr(_LogFingerprintScanner, "scan_log", scan_log)
    yield entered, release
    release.set()


@contextlib.asynccontextmanager
async def _runtime(asynchronous: bool) -> AsyncIterator[tuple[ContainerClient, Any, grpc.Channel]]:
    registry = StepRegistry({_STEP: container_step_for_binding(_BINDING)})
    with ThreadPoolExecutor(max_workers=8) as pool:
        if asynchronous:
            server = grpc.aio.server()
            service: Any = AsyncContainerRuntimeService(registry)
            container_pb2_grpc.add_ContainerServiceServicer_to_server(service, server)
        else:
            server = grpc.server(pool)
            service = add_container_runtime_service(
                server, registry, install_sigterm_shutdown=False
            )
        port = server.add_insecure_port("127.0.0.1:0")
        if asynchronous:
            await server.start()
        else:
            server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        try:
            yield ContainerClient(channel), service, channel
        finally:
            channel.close()
            if asynchronous:
                await server.stop(grace=0)
            else:
                server.stop(grace=0).wait()
            service.shutdown()


class _Outcome:
    def __init__(self) -> None:
        self.batches: list[ContainerLogBatch] = []
        self.result: ContainerResult | None = None
        self.failure: FailureCode | None = None
        self.source: str | None = None
        self.done = Event()

    @property
    def terminal(self) -> bool:
        return self.result is not None or self.failure is not None

    @property
    def messages(self) -> list[str]:
        assert len(self.batches) == 1
        return [record.message for record in self.batches[0].records]

    @property
    def dropped(self) -> int:
        assert len(self.batches) == 1
        return self.batches[0].dropped_count


def _consume(
    client: ContainerClient, invocation: ContainerInvocation, outcome: _Outcome,
    handler: Any = None,
) -> None:
    resolver = handler if handler is not None else (lambda _: {"token": _SECRET})
    try:
        for item in client.stream(invocation, credential_handler=resolver):
            if isinstance(item, ContainerLogBatch):
                outcome.batches.append(item)
            elif isinstance(item, ContainerResult):
                outcome.result = item
    except ContainerRemoteError as error:
        outcome.failure = error.code
        outcome.source = error.source
    finally:
        outcome.done.set()


def _invocation(mode: str, invocation_id: str = "cancel-1", timeout: float = 5) -> ContainerInvocation:
    return ContainerInvocation(invocation_id, _STEP, {"mode": mode}, timeout)


async def _wait(event: Event, timeout: float = 3) -> None:
    assert await asyncio.to_thread(event.wait, timeout)


def _no_secret(outcome: _Outcome) -> None:
    for batch in outcome.batches:
        for record in batch.records:
            assert _SECRET not in json.dumps(
                [record.message, record.event_name, record.attributes]
            )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("behaviour", ["cooperative", "blocking"])
def test_cancel_keeps_safe_records_when_nothing_was_read(
    asynchronous: bool, behaviour: str,
) -> None:
    async def scenario() -> None:
        async with _runtime(asynchronous) as (client, _service, _channel):
            outcome = _Outcome()
            thread = Thread(target=_consume, args=(client, _invocation(f"wait_{behaviour}"), outcome))
            thread.start()
            await _wait(ProbeStep.started)
            assert await asyncio.to_thread(client.cancel, "cancel-1")
            await _wait(outcome.done)
            ProbeStep.release.set()
            thread.join(3)
            assert outcome.failure is FailureCode.CANCELLED
            assert outcome.messages == ["safe before"]
            assert outcome.dropped == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("behaviour", ["cooperative", "blocking"])
def test_cancel_screens_records_against_credentials_read(
    asynchronous: bool, behaviour: str,
) -> None:
    async def scenario() -> None:
        async with _runtime(asynchronous) as (client, _service, _channel):
            outcome = _Outcome()
            thread = Thread(target=_consume, args=(client, _invocation(f"read_{behaviour}"), outcome))
            thread.start()
            await _wait(ProbeStep.started)
            assert await asyncio.to_thread(client.cancel, "cancel-1")
            await _wait(outcome.done)
            ProbeStep.release.set()
            thread.join(3)
            assert outcome.failure is FailureCode.CANCELLED
            assert outcome.messages == ["safe before", "safe after"]
            assert outcome.dropped == 1
            _no_secret(outcome)

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_timeout_screens_records_instead_of_discarding(asynchronous: bool) -> None:
    async def scenario() -> None:
        async with _runtime(asynchronous) as (client, _service, _channel):
            outcome = _Outcome()
            await asyncio.to_thread(
                _consume, client, _invocation("read_cooperative", timeout=1), outcome
            )
            assert outcome.failure is FailureCode.TIMEOUT
            assert outcome.messages == ["safe before", "safe after"]
            assert outcome.dropped == 1
            _no_secret(outcome)

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_read_after_cancellation_never_reaches_the_step(asynchronous: bool) -> None:
    async def scenario() -> None:
        async with _runtime(asynchronous) as (client, _service, _channel):
            outcome = _Outcome()
            thread = Thread(
                target=_consume, args=(client, _invocation("wait_then_read_cooperative"), outcome)
            )
            thread.start()
            await _wait(ProbeStep.started)
            assert await asyncio.to_thread(client.cancel, "cancel-1")
            await _wait(outcome.done)
            thread.join(3)
            for _ in range(100):
                if ProbeStep.observed:
                    break
                await asyncio.sleep(0.02)
            assert outcome.failure is FailureCode.CANCELLED
            assert ProbeStep.observed and ProbeStep.observed[0] != "read"
            _no_secret(outcome)

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("action", ["cancel", "shutdown"])
def test_cancellation_during_post_step_scan_delivers_prepared_outcome(
    asynchronous: bool, failure: bool, action: str, slow_scan: tuple[Event, Event],
) -> None:
    entered, release = slow_scan

    async def scenario() -> None:
        async with _runtime(asynchronous) as (client, service, _channel):
            outcome = _Outcome()
            mode = "fail" if failure else "plain"
            thread = Thread(target=_consume, args=(client, _invocation(mode), outcome))
            thread.start()
            await _wait(entered)
            if action == "cancel":
                assert await asyncio.to_thread(client.cancel, "cancel-1")
            else:
                service.shutdown()
            # Delivered while the scan is still blocked: cancellation never waits.
            await _wait(outcome.done)
            release.set()
            thread.join(3)
            if failure:
                assert outcome.failure is FailureCode.REMOTE_REJECTED
            else:
                assert outcome.failure is None
                assert outcome.result is not None
            assert outcome.messages == []
            assert outcome.dropped == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_unexpected_client_message_during_post_step_scan_keeps_prepared_outcome(
    asynchronous: bool, slow_scan: tuple[Event, Event],
) -> None:
    entered, release = slow_scan
    send_unexpected = Event()

    async def scenario() -> None:
        async with _runtime(asynchronous) as (_client, _service, channel):
            start = container_pb2.ExecuteClientMessage(
                start=_request_from_invocation(_invocation("plain"))
            )

            def requests() -> Iterator[container_pb2.ExecuteClientMessage]:
                yield start
                assert send_unexpected.wait(3)
                yield start

            def call() -> list[container_pb2.ExecuteServerMessage]:
                stub = container_pb2_grpc.ContainerServiceStub(channel)
                return list(stub.Execute(requests(), timeout=5))

            pending = asyncio.ensure_future(asyncio.to_thread(call))
            await _wait(entered)
            send_unexpected.set()
            done, _ = await asyncio.wait({pending}, timeout=2)
            release.set()
            assert done, "terminal must not wait for the in-progress scan"
            messages = pending.result()
            events = [message.event for message in messages if message.HasField("event")]
            assert len(events) == 1
            assert events[0].WhichOneof("event") == "result"

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_cancel_scan_shares_the_invocation_log_budget(
    asynchronous: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each record costs five scan nodes; twelve cover two records only.
    monkeypatch.setattr(credentials_module, "_MAX_OUTPUT_NODES", 12)

    async def scenario() -> None:
        async with _runtime(asynchronous) as (client, _service, _channel):
            outcome = _Outcome()
            thread = Thread(target=_consume, args=(client, _invocation("read_blocking"), outcome))
            thread.start()
            await _wait(ProbeStep.started)
            assert await asyncio.to_thread(client.cancel, "cancel-1")
            await _wait(outcome.done)
            ProbeStep.release.set()
            thread.join(3)
            assert outcome.failure is FailureCode.CANCELLED
            assert outcome.messages == ["safe before"]
            assert outcome.dropped == 2
            _no_secret(outcome)

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_cancel_discards_when_reads_cannot_be_revoked(
    asynchronous: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_scope: Any) -> Any:
        raise RuntimeError(_SECRET)

    monkeypatch.setattr(
        credentials_module._CredentialScope, "_freeze_log_fingerprints", broken, raising=False
    )

    async def scenario() -> None:
        async with _runtime(asynchronous) as (client, _service, _channel):
            outcome = _Outcome()
            thread = Thread(target=_consume, args=(client, _invocation("read_blocking"), outcome))
            thread.start()
            await _wait(ProbeStep.started)
            assert await asyncio.to_thread(client.cancel, "cancel-1")
            await _wait(outcome.done)
            ProbeStep.release.set()
            thread.join(3)
            assert outcome.failure is FailureCode.CANCELLED
            assert outcome.messages == []
            assert outcome.dropped == 3

    asyncio.run(scenario())


def test_async_cancel_scan_does_not_block_the_event_loop(slow_scan: tuple[Event, Event]) -> None:
    entered, release = slow_scan

    async def scenario() -> None:
        async with _runtime(True) as (client, _service, _channel):
            outcome = _Outcome()
            thread = Thread(target=_consume, args=(client, _invocation("read_blocking"), outcome))
            thread.start()
            await _wait(ProbeStep.started)
            ticks = 0

            async def ticker() -> None:
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0.01)

            ticking = asyncio.ensure_future(ticker())
            cancelling = asyncio.ensure_future(asyncio.to_thread(client.cancel, "cancel-1"))
            await _wait(entered)
            before = ticks
            await asyncio.sleep(0.3)
            assert ticks - before >= 10
            release.set()
            assert await cancelling
            ticking.cancel()
            await _wait(outcome.done)
            ProbeStep.release.set()
            thread.join(3)
            assert outcome.failure is FailureCode.CANCELLED
            assert outcome.messages == ["safe before", "safe after"]

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_exactly_one_terminal_when_cancel_races_completion(
    asynchronous: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections import Counter

    from syntara_plugin.runtime.credential_callback import CallbackCredentialProvider

    # Each queuing path writes batch, event and terminator together and the
    # stream stops at the first terminator, so counting delivered events alone
    # cannot reveal a second commit. Count successful gate claims instead.
    claims: Counter[str] = Counter()
    original_claim = CallbackCredentialProvider._claim_terminal

    def claim(provider: Any, fallback: Any) -> Any:
        event = original_claim(provider, fallback)
        if event is not None:
            claims[provider._context.invocation_id] += 1
        return event

    monkeypatch.setattr(CallbackCredentialProvider, "_claim_terminal", claim)

    async def scenario() -> None:
        async with _runtime(asynchronous) as (client, _service, channel):
            stub = container_pb2_grpc.ContainerServiceStub(channel)

            def execute(
                start: container_pb2.ExecuteClientMessage,
            ) -> list[container_pb2.ExecuteServerMessage]:
                return list(stub.Execute(iter([start]), timeout=5))

            for index in range(30):
                invocation_id = f"race-{index}"
                start = container_pb2.ExecuteClientMessage(
                    start=_request_from_invocation(_invocation("plain", invocation_id))
                )
                pending = asyncio.ensure_future(asyncio.to_thread(execute, start))
                await asyncio.sleep(random.uniform(0, 0.01))
                with contextlib.suppress(ContainerRemoteError):
                    await asyncio.to_thread(client.cancel, invocation_id)
                messages = await pending
                events = [message.event for message in messages if message.HasField("event")]
                batches = [message for message in messages if message.HasField("log_batch")]
                assert len(events) == 1
                assert len(batches) <= 1
            await asyncio.sleep(0.2)
            assert len(claims) == 30
            assert set(claims.values()) == {1}

    asyncio.run(scenario())


def _read_after_barrier(barrier: Barrier, handle: Any, read: list[bool]) -> None:
    barrier.wait()
    try:
        handle.read()
        read.append(True)
    except CredentialAccessError:
        read.append(False)


def test_freeze_orders_against_concurrent_reads(tmp_path: Path) -> None:
    path = tmp_path / "credential.json"
    path.write_text(json.dumps({"token": _SECRET}))
    for _ in range(200):
        context = ExecutionContext(
            "invocation",
            credential_provider=FileCredentialProvider({_NAME: str(path)}, local_directory=tmp_path),
        )._with_credentials((_NAME,))
        scope = context._credential_scope
        assert scope is not None
        handle = context.credential(_NAME)
        barrier = Barrier(2)
        read: list[bool] = []
        thread = Thread(target=_read_after_barrier, args=(barrier, handle, read))
        thread.start()
        barrier.wait()
        scanner = scope._freeze_log_fingerprints()
        thread.join(2)
        if read[0]:
            # A read that returned its document was fingerprinted first.
            assert scanner.scan_log(["INFO", f"x {_SECRET}", "", {}]) is False
        with pytest.raises(CredentialAccessError):
            handle.read()
        scope.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_step_that_loses_the_terminal_claim_leaves_records_to_the_winner(
    asynchronous: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from syntara_plugin.runtime.credential_callback import CallbackCredentialProvider

    step_lost = Event()
    original_prepare = CallbackCredentialProvider._prepare_terminal
    original_cancel_logs = CallbackCredentialProvider.cancel_logs

    def prepare(provider: Any, event: Any) -> bool:
        prepared = original_prepare(provider, event)
        if not prepared:
            step_lost.set()
        return prepared

    def cancel_logs(provider: Any) -> None:
        # Let the cancelled step return and lose the claim before screening.
        step_lost.wait(3)
        original_cancel_logs(provider)

    monkeypatch.setattr(CallbackCredentialProvider, "_prepare_terminal", prepare)
    monkeypatch.setattr(CallbackCredentialProvider, "cancel_logs", cancel_logs)

    async def scenario() -> None:
        async with _runtime(asynchronous) as (client, _service, _channel):
            outcome = _Outcome()
            thread = Thread(target=_consume, args=(client, _invocation("read_cooperative"), outcome))
            thread.start()
            await _wait(ProbeStep.started)
            assert await asyncio.to_thread(client.cancel, "cancel-1")
            await _wait(outcome.done)
            thread.join(3)
            assert step_lost.is_set()
            assert outcome.failure is FailureCode.CANCELLED
            assert outcome.messages == ["safe before", "safe after"]
            assert outcome.dropped == 1
            _no_secret(outcome)

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_cancel_freeze_after_scope_teardown_still_screens_secrets(
    asynchronous: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from syntara_plugin.runtime import execution as execution_module

    hook_taken, detached = Event(), Event()
    original_prepare = execution_module._prepare_deferred_logs
    original_freeze = credentials_module._CredentialScope._freeze_log_fingerprints

    def prepare(context: Any, scope: Any) -> None:
        # Force the order: cancellation has taken the freeze hook, then the
        # step's teardown detaches the fingerprints, then the freeze runs.
        hook_taken.wait(3)
        original_prepare(context, scope)
        detached.set()

    def freeze(scope: Any) -> Any:
        hook_taken.set()
        detached.wait(3)
        # The freeze must not see an emptied scope and accept every record.
        return original_freeze(scope)

    monkeypatch.setattr(execution_module, "_prepare_deferred_logs", prepare)
    monkeypatch.setattr(credentials_module._CredentialScope, "_freeze_log_fingerprints", freeze)

    async def scenario() -> None:
        async with _runtime(asynchronous) as (client, _service, _channel):
            outcome = _Outcome()
            thread = Thread(target=_consume, args=(client, _invocation("read_cooperative"), outcome))
            thread.start()
            await _wait(ProbeStep.started)
            assert await asyncio.to_thread(client.cancel, "cancel-1")
            await _wait(outcome.done)
            thread.join(3)
            assert hook_taken.is_set() and detached.is_set()
            assert outcome.failure is FailureCode.CANCELLED
            _no_secret(outcome)
            assert outcome.messages == ["safe before", "safe after"]
            assert outcome.dropped == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_cancellation_scan_is_bounded_in_wall_clock_time(
    asynchronous: bool, action: str, slow_scan: tuple[Event, Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from time import monotonic

    from syntara_plugin.runtime import toolbox as toolbox_module

    # The work budget bounds CPU, not latency: a scan that outlasts the limit
    # must not hold the terminal event back past the host's delivery window.
    monkeypatch.setattr(toolbox_module, "_CANCEL_SCAN_SECONDS", 0.3, raising=False)
    entered, release = slow_scan

    async def scenario() -> None:
        async with _runtime(asynchronous) as (client, _service, _channel):
            outcome = _Outcome()
            timeout = 1 if action == "timeout" else 5
            thread = Thread(
                target=_consume, args=(client, _invocation("read_blocking", timeout=timeout), outcome)
            )
            thread.start()
            await _wait(ProbeStep.started)
            started = monotonic()
            if action == "cancel":
                assert await asyncio.to_thread(client.cancel, "cancel-1")
            await _wait(entered)
            await _wait(outcome.done, timeout=2)
            elapsed = monotonic() - started
            release.set()
            ProbeStep.release.set()
            thread.join(3)
            expected = FailureCode.CANCELLED if action == "cancel" else FailureCode.TIMEOUT
            assert outcome.failure is expected
            assert outcome.source == "workload"
            assert elapsed < (1.0 if action == "cancel" else 2.0)
            assert outcome.messages == []
            assert outcome.dropped == 3
            _no_secret(outcome)

    asyncio.run(scenario())


def test_stopped_log_scanner_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "credential.json"
    path.write_text(json.dumps({"token": _SECRET}))
    context = ExecutionContext(
        "invocation",
        credential_provider=FileCredentialProvider({_NAME: str(path)}, local_directory=tmp_path),
    )._with_credentials((_NAME,))
    scope = context._credential_scope
    assert scope is not None
    context.credential(_NAME).read()
    scanner = scope._freeze_log_fingerprints()
    assert scanner.scan_log(["INFO", "safe", "", {}]) is True
    scanner.stop()
    assert scanner.scan_log(["INFO", "safe", "", {}]) is False
    scope.close()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_termination_during_an_inflight_read_fails_closed(asynchronous: bool, action: str) -> None:
    # The host withholds the credential response, so the read is still in
    # flight when the invocation ends. Its value was never fingerprinted, yet a
    # queued record already contains it: screening cannot be complete, so every
    # record must be dropped instead of scanned.
    entered, release = Event(), Event()

    def withheld(_context: Any) -> dict[str, str]:
        entered.set()
        release.wait(5)
        return {"token": _SECRET}

    async def scenario() -> None:
        async with _runtime(asynchronous) as (client, _service, _channel):
            outcome = _Outcome()
            timeout = 1 if action == "timeout" else 5
            thread = Thread(
                target=_consume,
                args=(client, _invocation("inflight_read", timeout=timeout), outcome, withheld),
            )
            thread.start()
            await _wait(entered)
            if action == "cancel":
                assert await asyncio.to_thread(client.cancel, "cancel-1")
            else:
                await asyncio.sleep(1.5)
            release.set()
            await _wait(outcome.done)
            thread.join(3)
            expected = FailureCode.CANCELLED if action == "cancel" else FailureCode.TIMEOUT
            assert outcome.failure is expected
            _no_secret(outcome)
            assert outcome.messages == []
            assert outcome.dropped == 2

    asyncio.run(scenario())
