"""Tests for the in-container runtime server bootstrap."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import grpc
import pytest
from pydantic import BaseModel
from syntara_plugin.contracts import canonical_json_digest
from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    ContainerClient,
    ContainerInvocation,
    ContainerInvocationError,
    ContainerRemoteError,
    ContainerResult,
    ExecutionContext,
    FailureCode,
    InstalledStepManifest,
    StepBindings,
    StepDescriptor,
    StepRegistry,
)
from syntara_plugin.runtime.adapter import step_registry_from_bindings
from syntara_plugin.runtime.protocol import container_pb2, container_pb2_grpc
from syntara_plugin.runtime.server import (
    AsyncContainerRuntimeService,
    RuntimeServerConfig,
    RuntimeServerError,
    _DaemonStepWorkerPool,
    create_runtime_server,
    load_runtime_bindings,
    load_runtime_server_config,
    resolved_step_registry,
)

REVISION = "sha256:" + "a" * 64


class EchoInput(BaseModel):
    message: str


class EchoOutput(BaseModel):
    message: str


class EchoStep(ActionStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        return EchoOutput(message=inputs.message)


EchoAlias = EchoStep


class BarrierStep(ActionStep[EchoInput, EchoOutput]):
    barrier = threading.Barrier(2, timeout=2)

    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        self.barrier.wait()
        return EchoOutput(message=inputs.message)


class CancellationStep(ActionStep[EchoInput, EchoOutput]):
    started = threading.Event()
    cancelled = threading.Event()

    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        self.started.set()
        if context.cancellation.wait(timeout=5):
            self.cancelled.set()
        return EchoOutput(message=inputs.message)


class CooperativeTimeoutStep(ActionStep[EchoInput, EchoOutput]):
    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        context.cancellation.wait(timeout=5)
        return EchoOutput(message=inputs.message)


class UncancellableTimeoutStep(ActionStep[EchoInput, EchoOutput]):
    release = threading.Event()
    finished = threading.Event()

    def run(self, inputs: EchoInput, context: ExecutionContext) -> EchoOutput:
        del context
        self.release.wait(timeout=5)
        self.finished.set()
        return EchoOutput(message=inputs.message)


class FakeServicerContext:
    def add_done_callback(self, callback: object) -> None:
        del callback


class FailingServicerContext(FakeServicerContext):
    def add_done_callback(self, callback: object) -> None:
        del callback
        raise RuntimeError("callback registration failed")


def installed_action_bindings(
    step_identity: str,
    step: BaseStep[EchoInput, EchoOutput],
    *,
    input_schema: Mapping[str, Any] | None = None,
) -> StepBindings:
    step_class = type(step)
    installed_step = InstalledStepManifest.from_manifest(
        step_identity,
        {
            "apiVersion": "syntara.io/v1alpha1",
            "kind": "Action",
            "metadata": {
                "name": "echo",
                "displayName": "Echo",
                "description": "Echoes one message.",
            },
            "spec": {
                "runtime": {
                    "kind": "custom-runner",
                    "entryPoint": f"{step_class.__module__}:{step_class.__qualname__}",
                },
                "input": {
                    "kind": "inline",
                    "value": input_schema if input_schema is not None else EchoInput.model_json_schema(),
                },
                "output": {"kind": "inline", "value": EchoOutput.model_json_schema()},
            },
        },
        {},
    )
    bindings = StepBindings()
    bindings.bind(StepDescriptor.for_installed_step(step, installed_step), step)
    return bindings


def create_bindings() -> StepBindings:
    return installed_action_bindings("smoke.echo.v1", EchoStep())


def create_barrier_bindings() -> StepBindings:
    return installed_action_bindings("smoke.barrier.v1", BarrierStep())


def create_cancellation_bindings() -> StepBindings:
    CancellationStep.started = threading.Event()
    CancellationStep.cancelled = threading.Event()
    return installed_action_bindings("smoke.cancellation.v1", CancellationStep())


def create_cooperative_timeout_bindings() -> StepBindings:
    return installed_action_bindings("smoke.cooperative-timeout.v1", CooperativeTimeoutStep())


def create_uncancellable_timeout_bindings() -> StepBindings:
    return installed_action_bindings("smoke.uncancellable-timeout.v1", UncancellableTimeoutStep())


def _schema_digests(input_schema: Mapping[str, Any] | None = None) -> dict[str, str]:
    return {
        "input": canonical_json_digest(input_schema or EchoInput.model_json_schema()),
        "output": canonical_json_digest(EchoOutput.model_json_schema()),
    }


def write_runtime_config(
    tmp_path: Path,
    *,
    abi: str = "syntara.container/v1alpha1",
    adapter: object | None = None,
) -> Path:
    document = {
        "abi": abi,
        "adapter": adapter if adapter is not None else {"bindingsFactory": "example:create"},
    }
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def not_bindings() -> object:
    return object()


def empty_bindings() -> StepBindings:
    return StepBindings()


def failing_bindings() -> StepBindings:
    raise RuntimeError("factory exploded")


def resolved_file(tmp_path: Path, step_class: type[BaseStep[EchoInput, EchoOutput]]) -> Path:
    identities = {
        EchoStep: "smoke.echo.v1",
        BarrierStep: "smoke.barrier.v1",
        CancellationStep: "smoke.cancellation.v1",
    }
    path = tmp_path / "resolved.json"
    path.write_text(json.dumps({"bindings": [{
        "stepRevisionId": REVISION,
        "stepIdentity": identities[step_class],
        "runtimeKind": "platform-runner",
        "entryPoint": f"{step_class.__module__}:{step_class.__qualname__}",
        "abi": "syntara.container/v1alpha1",
        "schemaDigests": _schema_digests(),
    }]}), encoding="utf-8")
    return path


def _invocation() -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
        step_revision_id=REVISION,
        inputs={"message": "hello"},
        timeout_seconds=30,
        runtime_context={},
    )


def _barrier_invocation(invocation_id: str) -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id=invocation_id,
        workflow_run_id="workflow-run-1",
        step_revision_id=REVISION,
        inputs={"message": invocation_id},
        timeout_seconds=30,
        runtime_context={},
    )


def _cancellation_invocation() -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
        step_revision_id=REVISION,
        inputs={"message": "cancelled"},
        timeout_seconds=30,
        runtime_context={},
    )


def _timeout_invocation(step_revision_id: str) -> ContainerInvocation:
    return ContainerInvocation(
        invocation_id="invocation-1",
        workflow_run_id="workflow-run-1",
        step_revision_id=step_revision_id,
        inputs={"message": "timeout"},
        timeout_seconds=1,
        runtime_context={},
    )


async def _service_events(
    service: AsyncContainerRuntimeService,
    invocation: ContainerInvocation,
    *,
    release: threading.Event | None = None,
) -> list[container_pb2.ExecutionEvent]:
    request = container_pb2.ExecuteRequest(
        invocation_id=invocation.invocation_id,
        workflow_run_id=invocation.workflow_run_id,
        step_revision_id=invocation.step_revision_id,
        runtime_abi="syntara.container/v1alpha1",
        inputs_json=json.dumps({"message": invocation.inputs["message"]}).encode(),
        timeout_seconds=invocation.timeout_seconds,
    )
    async def requests():
        yield container_pb2.ExecuteClientMessage(start=request)
        await asyncio.Event().wait()

    events = [message.event async for message in service.Execute(requests(), FakeServicerContext())]
    if release is not None:
        release.set()
    return events


def _run_openssl(*arguments: str) -> None:
    subprocess.run(("openssl", *arguments), check=True, capture_output=True)


def _generate_tls_material(
    directory: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    ca_key = directory / "ca.key"
    ca_certificate = directory / "ca.crt"
    _run_openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca_certificate),
        "-days",
        "1",
        "-subj",
        "/CN=runtime-test-ca",
    )

    certificates: dict[str, Path] = {}
    keys: dict[str, Path] = {}
    for role in ("server", "client"):
        key = directory / f"{role}.key"
        request = directory / f"{role}.csr"
        certificate = directory / f"{role}.crt"
        _run_openssl(
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(request),
            "-subj",
            f"/CN=runtime-test-{role}",
        )
        command = [
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(ca_certificate),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(certificate),
            "-days",
            "1",
            "-sha256",
        ]
        if role == "server":
            extensions = directory / "server.ext"
            extensions.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n", encoding="utf-8")
            command.extend(("-extfile", str(extensions)))
        _run_openssl(*command)
        certificates[role] = certificate
        keys[role] = key

    return (
        certificates["server"],
        keys["server"],
        ca_certificate,
        certificates["client"],
        keys["client"],
    )


def test_runtime_server_serves_health_and_executes_a_bound_step(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    certificate, key, client_ca, client_certificate, client_key = _generate_tls_material(tmp_path)

    async def scenario() -> None:
        server = create_runtime_server(
            replace(
                _resolved_config(tmp_path, [_resolved_record()]), address=f"localhost:{port}",
                tls_certificate=certificate, tls_key=key, tls_client_ca=client_ca,
            )
        )
        await server.start()
        channel = grpc.secure_channel(
            f"localhost:{port}", grpc.ssl_channel_credentials(
                root_certificates=client_ca.read_bytes(),
                private_key=client_key.read_bytes(), certificate_chain=client_certificate.read_bytes(),
            )
        )
        try:
            client = ContainerClient(channel)
            health = await asyncio.to_thread(client.health)
            result = await asyncio.to_thread(client.execute, _invocation())

            assert health == (REVISION,)
            assert result.output == {"message": "hello"}
            unknown_invocation = replace(
                _invocation(), step_revision_id="sha256:" + "c" * 64
            )
            with pytest.raises(ContainerRemoteError) as raised:
                await asyncio.to_thread(client.execute, unknown_invocation)
            assert raised.value.code is FailureCode.NOT_FOUND
        finally:
            channel.close()
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_runtime_server_executes_steps_concurrently(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    certificate, key, client_ca, client_certificate, client_key = _generate_tls_material(tmp_path)

    async def scenario() -> None:
        server = create_runtime_server(
            RuntimeServerConfig(
                bindings_factory=f"{__name__}:create_barrier_bindings",
                resolved_execution_path=resolved_file(tmp_path, BarrierStep),
                address=f"localhost:{port}",
                tls_certificate=certificate, tls_key=key, tls_client_ca=client_ca,
            )
        )
        await server.start()
        channel = grpc.secure_channel(
            f"localhost:{port}", grpc.ssl_channel_credentials(
                root_certificates=client_ca.read_bytes(),
                private_key=client_key.read_bytes(), certificate_chain=client_certificate.read_bytes(),
            )
        )
        try:
            client = ContainerClient(channel)
            results = await asyncio.gather(
                asyncio.to_thread(client.execute, _barrier_invocation("invocation-1")),
                asyncio.to_thread(client.execute, _barrier_invocation("invocation-2")),
            )

            assert [result.output for result in results] == [
                {"message": "invocation-1"},
                {"message": "invocation-2"},
            ]
        finally:
            channel.close()
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_runtime_server_cancels_an_active_step(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    certificate, key, client_ca, client_certificate, client_key = _generate_tls_material(tmp_path)

    async def scenario() -> None:
        server = create_runtime_server(
            RuntimeServerConfig(
                bindings_factory=f"{__name__}:create_cancellation_bindings",
                resolved_execution_path=resolved_file(tmp_path, CancellationStep),
                address=f"localhost:{port}",
                tls_certificate=certificate, tls_key=key, tls_client_ca=client_ca,
            )
        )
        await server.start()
        channel = grpc.secure_channel(
            f"localhost:{port}", grpc.ssl_channel_credentials(
                root_certificates=client_ca.read_bytes(),
                private_key=client_key.read_bytes(), certificate_chain=client_certificate.read_bytes(),
            )
        )
        try:
            client = ContainerClient(channel)
            execution = asyncio.create_task(
                asyncio.to_thread(client.execute, _cancellation_invocation())
            )
            assert await asyncio.to_thread(CancellationStep.started.wait, 5)
            assert await asyncio.to_thread(client.cancel, "invocation-1", timeout_seconds=5)
            with pytest.raises(ContainerRemoteError) as raised:
                await execution
        finally:
            channel.close()
            await server.stop(grace=0)

        assert raised.value.code is FailureCode.CANCELLED
        assert raised.value.outcome == "not_completed"

    asyncio.run(scenario())


def test_runtime_server_cancels_step_when_client_disconnects(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    certificate, key, client_ca, client_certificate, client_key = _generate_tls_material(tmp_path)

    async def scenario() -> None:
        server = create_runtime_server(
            RuntimeServerConfig(
                bindings_factory=f"{__name__}:create_cancellation_bindings",
                resolved_execution_path=resolved_file(tmp_path, CancellationStep),
                address=f"localhost:{port}",
                tls_certificate=certificate, tls_key=key, tls_client_ca=client_ca,
            )
        )
        await server.start()
        channel = grpc.secure_channel(
            f"localhost:{port}", grpc.ssl_channel_credentials(
                root_certificates=client_ca.read_bytes(),
                private_key=client_key.read_bytes(), certificate_chain=client_certificate.read_bytes(),
            )
        )
        try:
            stub = container_pb2_grpc.ContainerServiceStub(channel)
            invocation = _cancellation_invocation()
            request = container_pb2.ExecuteRequest(
                invocation_id=invocation.invocation_id,
                workflow_run_id=invocation.workflow_run_id,
                step_revision_id=invocation.step_revision_id,
                runtime_abi="syntara.container/v1alpha1",
                inputs_json=json.dumps({"message": "disconnect"}).encode(),
                timeout_seconds=30,
            )
            def requests():
                yield container_pb2.ExecuteClientMessage(start=request)
                CancellationStep.cancelled.wait(10)

            call = stub.Execute(requests())
            execution = asyncio.create_task(asyncio.to_thread(list, call))
            assert await asyncio.to_thread(CancellationStep.started.wait, 5)

            call.cancel()
            with pytest.raises(grpc.RpcError) as raised:
                await execution

            assert raised.value.code() is grpc.StatusCode.CANCELLED
            assert await asyncio.to_thread(CancellationStep.cancelled.wait, 5)
        finally:
            channel.close()
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_runtime_server_times_out_a_cooperative_step() -> None:
    service = AsyncContainerRuntimeService(
        step_registry_from_bindings(create_cooperative_timeout_bindings())
    )

    events = asyncio.run(
        _service_events(service, _timeout_invocation("smoke.cooperative-timeout.v1"))
    )

    assert len(events) == 1
    assert events[0].WhichOneof("event") == "failure"
    assert events[0].failure.code == FailureCode.TIMEOUT
    assert events[0].failure.retryable is True
    assert events[0].failure.outcome == "not_completed"
    cancellation_response = asyncio.run(
        service.Cancel(
            container_pb2.CancelRequest(invocation_id="invocation-1"),
            FakeServicerContext(),
        )
    )
    assert cancellation_response.accepted is False


def test_async_execute_propagates_unexpected_setup_errors() -> None:
    service = AsyncContainerRuntimeService(
        StepRegistry({REVISION: lambda _invocation: ContainerResult(REVISION, output={})})
    )
    invocation = _invocation()
    request = container_pb2.ExecuteRequest(
        invocation_id=invocation.invocation_id,
        workflow_run_id=invocation.workflow_run_id,
        step_revision_id=invocation.step_revision_id,
        runtime_abi="syntara.container/v1alpha1",
        inputs_json=json.dumps(invocation.inputs).encode(),
        timeout_seconds=invocation.timeout_seconds,
    )

    async def requests():
        yield container_pb2.ExecuteClientMessage(start=request)

    async def scenario() -> None:
        messages = service.Execute(requests(), FailingServicerContext())
        with pytest.raises(RuntimeError, match="callback registration failed"):
            _ = [message async for message in messages]
        assert service._active == {}
        service.shutdown()

    asyncio.run(scenario())


def test_runtime_server_times_out_an_uncancellable_step() -> None:
    service = AsyncContainerRuntimeService(
        step_registry_from_bindings(create_uncancellable_timeout_bindings())
    )

    events = asyncio.run(
        _service_events(
            service,
            _timeout_invocation("smoke.uncancellable-timeout.v1"),
            release=UncancellableTimeoutStep.release,
        )
    )

    assert len(events) == 1
    assert events[0].WhichOneof("event") == "failure"
    assert events[0].failure.code == FailureCode.TIMEOUT
    assert events[0].failure.outcome == "not_completed"

    assert UncancellableTimeoutStep.finished.wait(timeout=5)
    cancellation_response = asyncio.run(
        service.Cancel(
            container_pb2.CancelRequest(invocation_id="invocation-1"),
            FakeServicerContext(),
        )
    )
    assert cancellation_response.accepted is False


@pytest.mark.parametrize(
    ("worker_limit", "failed_start_attempt"),
    ((2, 1), (3, 2)),
)
def test_daemon_worker_pool_retries_after_thread_start_failure(
    monkeypatch: pytest.MonkeyPatch,
    worker_limit: int,
    failed_start_attempt: int,
) -> None:
    pool = _DaemonStepWorkerPool(worker_limit)
    start_attempts = 0
    original_start = threading.Thread.start

    def start_with_one_failure(thread: threading.Thread) -> None:
        nonlocal start_attempts
        if thread.name.startswith("syntara-step_"):
            start_attempts += 1
            if start_attempts == failed_start_attempt:
                raise RuntimeError("simulated thread start failure")
        original_start(thread)

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        return ContainerResult(invocation.step_revision_id, output={"ok": True})

    monkeypatch.setattr(threading.Thread, "start", start_with_one_failure)
    try:
        invocation = _invocation()
        with pytest.raises(RuntimeError, match="simulated thread start failure"):
            pool.submit(handler, invocation)

        result = pool.submit(handler, invocation).result(timeout=2)

        assert result.output == {"ok": True}
        assert start_attempts == worker_limit + 1
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        for thread in pool._threads:
            if thread.ident is not None:
                thread.join(timeout=2)
                assert not thread.is_alive()


def test_daemon_worker_pool_shutdown_wait_after_partial_thread_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _DaemonStepWorkerPool(3)
    start_attempts = 0
    joined_threads: list[str] = []
    original_start = threading.Thread.start
    original_join = threading.Thread.join

    def start_with_one_failure(thread: threading.Thread) -> None:
        nonlocal start_attempts
        if thread.name.startswith("syntara-step_"):
            start_attempts += 1
            if start_attempts == 2:
                raise RuntimeError("simulated thread start failure")
        original_start(thread)

    def join_only_started_threads(
        thread: threading.Thread,
        timeout: float | None = None,
    ) -> None:
        if thread.name.startswith("syntara-step_"):
            assert thread.ident is not None, "shutdown attempted to join an unstarted worker"
            joined_threads.append(thread.name)
        original_join(thread, timeout=timeout)

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        return ContainerResult(invocation.step_revision_id, output={"ok": True})

    monkeypatch.setattr(threading.Thread, "start", start_with_one_failure)
    monkeypatch.setattr(threading.Thread, "join", join_only_started_threads)

    invocation = _invocation()
    with pytest.raises(RuntimeError, match="simulated thread start failure"):
        pool.submit(handler, invocation)

    assert start_attempts == 2
    started_threads = [thread.name for thread in pool._threads if thread.ident is not None]
    assert started_threads == ["syntara-step_0"]

    pool.shutdown(wait=True)

    assert joined_threads == started_threads
    assert all(thread.ident is None for thread in pool._threads if thread.name not in started_threads)
    assert all(not thread.is_alive() for thread in pool._threads if thread.name in started_threads)


def test_async_runtime_bounds_workers_after_repeated_timeouts() -> None:
    worker_limit = 8
    started_count = 0
    started_lock = threading.Lock()
    workers_started = threading.Event()
    workers_finished = threading.Event()
    release = threading.Event()
    finished_count = 0

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        nonlocal finished_count, started_count
        del invocation
        with started_lock:
            started_count += 1
            if started_count == worker_limit:
                workers_started.set()
        release.wait()
        with started_lock:
            finished_count += 1
            if finished_count == worker_limit:
                workers_finished.set()
        return ContainerResult(REVISION, output={"ok": True})

    service = AsyncContainerRuntimeService(StepRegistry({"example.step": handler}))
    invocation = replace(_invocation(), step_revision_id="example.step", timeout_seconds=0.5)

    async def scenario() -> None:
        tasks = [
            asyncio.create_task(
                service._execute_step(handler, replace(invocation, invocation_id=f"invocation-{index}"))
            )
            for index in range(worker_limit)
        ]
        try:
            assert await asyncio.to_thread(workers_started.wait, 2)
            timed_out = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(
                isinstance(outcome, ContainerInvocationError)
                and outcome.code is FailureCode.TIMEOUT
                for outcome in timed_out
            )
            assert started_count == worker_limit
            assert finished_count == 0

            rejected = await asyncio.gather(*[
                service._execute_step(
                    handler,
                    replace(invocation, invocation_id=f"rejected-{index}", timeout_seconds=2),
                )
                for index in range(worker_limit)
            ], return_exceptions=True)
            assert all(
                isinstance(outcome, ContainerInvocationError)
                and outcome.code is FailureCode.RATE_LIMITED
                for outcome in rejected
            )
            assert started_count == worker_limit
            assert finished_count == 0

            release.set()
            assert await asyncio.to_thread(workers_finished.wait, 2)
            deadline = asyncio.get_running_loop().time() + 2
            while True:
                try:
                    result = await service._execute_step(
                        handler,
                        replace(invocation, invocation_id="after-release", timeout_seconds=2),
                    )
                    break
                except ContainerInvocationError as error:
                    if error.code is not FailureCode.RATE_LIMITED:
                        raise
                    if asyncio.get_running_loop().time() >= deadline:
                        raise AssertionError("worker capacity did not recover after handlers returned") from error
                    await asyncio.sleep(0.01)
            assert result.output == {"ok": True}
            assert started_count == worker_limit + 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            service.shutdown()

    asyncio.run(scenario())


def test_async_runtime_shutdown_does_not_wait_for_blocked_handlers() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    source_paths = (
        repository_root / "sdks/python/packages/contracts/src",
        repository_root / "sdks/python/packages/sdk/src",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((
        *(str(path) for path in source_paths),
        environment.get("PYTHONPATH", ""),
    ))
    script = """\
import asyncio
import threading

from syntara_plugin.runtime import ContainerInvocation, ContainerInvocationError, ContainerResult, FailureCode, StepRegistry
from syntara_plugin.runtime.server import AsyncContainerRuntimeService

handler_started = threading.Event()
release_handler = threading.Event()

def blocked_handler(invocation):
    handler_started.set()
    release_handler.wait()
    return ContainerResult(invocation.step_revision_id, output={})

service = AsyncContainerRuntimeService(StepRegistry({"blocked": blocked_handler}))
invocation = ContainerInvocation(
    invocation_id="shutdown-test",
    workflow_run_id="workflow-run",
    step_revision_id="blocked",
    inputs={},
    timeout_seconds=1,
    runtime_context={},
)

async def run_step():
    task = asyncio.create_task(service._execute_step(blocked_handler, invocation))
    if not await asyncio.to_thread(handler_started.wait, 2):
        raise AssertionError("blocked handler did not start")
    try:
        await task
    except ContainerInvocationError as error:
        if error.code is not FailureCode.TIMEOUT:
            raise
    else:
        raise AssertionError("blocked handler did not time out")
    if release_handler.is_set():
        raise AssertionError("handler was released before shutdown")
    service.shutdown()
    release_handler.set()

asyncio.run(run_step())
print("shutdown complete", flush=True)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        check=True,
        text=True,
        timeout=3,
    )

    assert "shutdown complete" in completed.stdout


def test_runtime_server_serves_health_and_steps_over_mutual_tls(
    tmp_path: Path,
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    certificate, key, client_ca, client_certificate, client_key = _generate_tls_material(tmp_path)

    async def scenario() -> None:
        server = create_runtime_server(
            RuntimeServerConfig(
                bindings_factory=f"{__name__}:create_bindings",
                resolved_execution_path=resolved_file(tmp_path, EchoStep),
                address=f"localhost:{port}",
                tls_certificate=certificate,
                tls_key=key,
                tls_client_ca=client_ca,
            )
        )
        await server.start()
        credentials = grpc.ssl_channel_credentials(
            root_certificates=client_ca.read_bytes(),
            private_key=client_key.read_bytes(),
            certificate_chain=client_certificate.read_bytes(),
        )
        channel = grpc.secure_channel(f"localhost:{port}", credentials)
        try:
            client = ContainerClient(channel)
            health = await asyncio.to_thread(client.health, timeout_seconds=5)
            result = await asyncio.to_thread(client.execute, _invocation(), timeout_seconds=5)

            assert health == (REVISION,)
            assert result.output == {"message": "hello"}
        finally:
            channel.close()
            await server.stop(grace=0)

    asyncio.run(scenario())


def test_runtime_server_configuration_accepts_an_uppercase_loopback_host(
    tmp_path: Path,
) -> None:
    config_path = write_runtime_config(tmp_path)
    config = load_runtime_server_config(
        {
            "SYNTARA_RUNTIME_CONFIG": str(config_path),
            "SYNTARA_RUNTIME_GRPC_ADDRESS": "LOCALHOST:50051",
            "SYNTARA_RUNTIME_GRPC_TLS_CERT": "/tmp/cert",
            "SYNTARA_RUNTIME_GRPC_TLS_KEY": "/tmp/key",
            "SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA": "/tmp/ca",
        }
    )

    assert config.address == "LOCALHOST:50051"


@pytest.mark.parametrize("address", ["::1", "grpc://localhost:50051", "localhost:²"])
def test_runtime_server_configuration_rejects_non_host_port_addresses(
    address: str,
    tmp_path: Path,
) -> None:
    config_path = write_runtime_config(tmp_path)
    with pytest.raises(RuntimeServerError, match="host:port"):
        load_runtime_server_config(
            {
                "SYNTARA_RUNTIME_CONFIG": str(config_path),
                "SYNTARA_RUNTIME_GRPC_ADDRESS": address,
            }
        )


def test_runtime_server_configuration_requires_a_config_path() -> None:
    with pytest.raises(RuntimeServerError, match="RUNTIME_CONFIG"):
        load_runtime_server_config({})


def test_runtime_server_configuration_rejects_an_invalid_address(
    tmp_path: Path,
) -> None:
    config_path = write_runtime_config(tmp_path)
    with pytest.raises(RuntimeServerError, match="host:port"):
        load_runtime_server_config(
            {
                "SYNTARA_RUNTIME_CONFIG": str(config_path),
                "SYNTARA_RUNTIME_GRPC_ADDRESS": "localhost:not-a-port",
            }
        )


def test_non_loopback_runtime_listener_requires_mutual_tls(tmp_path: Path) -> None:
    config_path = write_runtime_config(tmp_path)
    with pytest.raises(RuntimeServerError, match="mutual TLS"):
        load_runtime_server_config(
            {
                "SYNTARA_RUNTIME_CONFIG": str(config_path),
                "SYNTARA_RUNTIME_GRPC_ADDRESS": "0.0.0.0:50051",
            }
        )


def test_runtime_tls_material_must_be_complete(tmp_path: Path) -> None:
    config_path = write_runtime_config(tmp_path)
    with pytest.raises(RuntimeServerError, match="certificate, key, and client CA"):
        load_runtime_server_config(
            {
                "SYNTARA_RUNTIME_CONFIG": str(config_path),
                "SYNTARA_RUNTIME_GRPC_TLS_CERT": "/tmp/tls.crt",
            }
        )


def test_runtime_config_rejects_the_wrong_abi(tmp_path: Path) -> None:
    config_path = write_runtime_config(tmp_path, abi="syntara.container/v0")
    with pytest.raises(RuntimeServerError, match="ABI"):
        load_runtime_server_config(config_path=config_path)


def test_runtime_config_rejects_a_language_field(
    tmp_path: Path,
) -> None:
    config_path = write_runtime_config(tmp_path)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    document["language"] = "python"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RuntimeServerError, match="exactly abi and adapter"):
        load_runtime_server_config(config_path=config_path)


def test_runtime_config_rejects_an_invalid_python_adapter(tmp_path: Path) -> None:
    config_path = write_runtime_config(tmp_path, adapter={"factory": "example:create"})
    with pytest.raises(RuntimeServerError, match="bindingsFactory"):
        load_runtime_server_config(config_path=config_path)


def test_runtime_bindings_factory_must_use_module_syntax() -> None:
    with pytest.raises(RuntimeServerError, match="module:factory"):
        load_runtime_bindings("example.create")


def test_runtime_bindings_factory_must_return_step_bindings() -> None:
    with pytest.raises(RuntimeServerError, match="return StepBindings"):
        load_runtime_bindings(f"{__name__}:not_bindings")


def test_runtime_bindings_factory_must_register_a_step() -> None:
    with pytest.raises(RuntimeServerError, match="at least one step type"):
        load_runtime_bindings(f"{__name__}:empty_bindings")


def test_runtime_bindings_factory_failure_stays_bounded() -> None:
    with pytest.raises(RuntimeServerError) as exc_info:
        load_runtime_bindings(f"{__name__}:failing_bindings")

    message = str(exc_info.value)
    assert "could not be loaded" in message
    assert "factory exploded" not in message
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def _resolved_record(kind: str = "custom-runner") -> dict[str, object]:
    return {
        "stepRevisionId": REVISION,
        "stepIdentity": "smoke.echo.v1",
        "runtimeKind": kind,
        "entryPoint": f"{__name__}:EchoStep",
        "abi": "syntara.container/v1alpha1",
        "schemaDigests": _schema_digests(),
    }


def _resolved_config(
    tmp_path: Path, records: list[dict[str, object]], *, manifest: dict[str, object] | None = None
) -> RuntimeServerConfig:
    resolved = tmp_path / "resolved.json"
    resolved.write_text(json.dumps({"bindings": records}), encoding="utf-8")
    registration = tmp_path / "registration.json"
    if manifest is None:
        manifest = {
            "apiVersion": "syntara.registration/v1alpha1",
            "kind": "RegistrationManifest",
            "plugin_revision_id": "sha256:" + "b" * 64,
            "sdkVersion": "0.1.0",
            "workload": "plugin-workload",
            "steps": {REVISION: {
                "stepIdentity": "smoke.echo.v1",
                "entryPoint": f"{__name__}:EchoStep",
                "abi": "syntara.container/v1alpha1",
                "schemaDigests": _schema_digests(),
            }},
        }
    registration.write_text(json.dumps(manifest), encoding="utf-8")
    return RuntimeServerConfig(
        bindings_factory=f"{__name__}:create_bindings",
        resolved_execution_path=resolved,
        registration_manifest_path=registration,
    )


def test_custom_resolved_revision_dispatches_with_installed_schema(tmp_path: Path) -> None:
    config = _resolved_config(tmp_path, [_resolved_record()])
    registry = resolved_step_registry(config, create_bindings())

    assert registry.step_revision_ids == (REVISION,)
    assert registry.resolve(REVISION)(_invocation()).output == {"message": "hello"}
    invalid = replace(_invocation(), inputs={})
    result = registry.resolve(REVISION)(invalid)
    assert result.error_result is not None
    assert result.error_result["code"] == "INVALID_INPUT"
    with pytest.raises(ContainerInvocationError) as error:
        registry.resolve("smoke.echo.v1")
    assert error.value.code is FailureCode.NOT_FOUND


def test_platform_resolved_revision_needs_no_registration_manifest(tmp_path: Path) -> None:
    config = _resolved_config(tmp_path, [_resolved_record("platform-runner")])
    config.registration_manifest_path.unlink()
    registry = resolved_step_registry(config, create_bindings())
    assert registry.step_revision_ids == (REVISION,)
    assert registry.resolve(REVISION)(_invocation()).output == {"message": "hello"}


def test_server_requires_resolved_execution_data() -> None:
    config = RuntimeServerConfig(f"{__name__}:create_bindings")
    bindings = create_bindings()
    with pytest.raises(RuntimeServerError, match="RESOLVED_EXECUTION"):
        resolved_step_registry(config, bindings)


@pytest.mark.parametrize(
        ("changes", "message"),
    [
        ({"stepRevisionId": "smoke.echo.v1"}, "contract"),
        ({"runtimeKind": "other"}, "contract"),
        ({"abi": "syntara.container/v0"}, "contract"),
        ({"entryPoint": "bad-entry"}, "contract"),
        ({"entryPoint": "missing.module:Step"}, "could not be loaded"),
        ({"entryPoint": f"{__name__}:EchoInput"}, "step type"),
        ({"entryPoint": f"{__name__}:BarrierStep"}, "does not match"),
        ({"entryPoint": f"{__name__}:EchoAlias"}, "invalid installed binding"),
        ({"extra": "field"}, "contract"),
    ],
)
def test_invalid_resolved_bindings_fail_startup(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    record = _resolved_record("platform-runner") | changes
    config = _resolved_config(tmp_path, [record])
    bindings = create_bindings()
    with pytest.raises(RuntimeServerError, match=message):
        resolved_step_registry(config, bindings)


def test_duplicate_resolved_revision_fails_startup(tmp_path: Path) -> None:
    record = _resolved_record("platform-runner")
    config = _resolved_config(tmp_path, [record, record])
    bindings = create_bindings()
    with pytest.raises(RuntimeServerError, match="duplicate step type revision"):
        resolved_step_registry(config, bindings)


def test_duplicate_json_keys_fail_startup(tmp_path: Path) -> None:
    config = _resolved_config(tmp_path, [_resolved_record()])
    assert config.resolved_execution_path is not None
    config.resolved_execution_path.write_text('{"bindings": [], "bindings": []}', encoding="utf-8")
    bindings = create_bindings()
    with pytest.raises(RuntimeServerError, match="valid resolved execution"):
        resolved_step_registry(config, bindings)


def test_duplicate_registration_keys_fail_startup(tmp_path: Path) -> None:
    config = _resolved_config(tmp_path, [_resolved_record()])
    config.registration_manifest_path.write_text('{"steps": {}, "steps": {}}', encoding="utf-8")
    bindings = create_bindings()
    with pytest.raises(RuntimeServerError, match="valid registration manifest"):
        resolved_step_registry(config, bindings)


def test_entry_point_import_failure_is_reported_at_startup(tmp_path: Path, monkeypatch) -> None:
    config = _resolved_config(tmp_path, [_resolved_record("platform-runner")])
    bindings = create_bindings()
    original_import = importlib.import_module

    def fail_import(module: str) -> None:
        if module == __name__:
            raise RuntimeError("import failed")
        return original_import(module)

    monkeypatch.setattr("syntara_plugin.runtime.server.importlib.import_module", fail_import)
    with pytest.raises(RuntimeServerError, match="could not be loaded"):
        resolved_step_registry(config, bindings)


@pytest.mark.parametrize("change", ["revision", "identity", "entry", "abi", "schemas", "malformed"])
def test_custom_record_must_match_packaged_registration(tmp_path: Path, change: str) -> None:
    config = _resolved_config(tmp_path, [_resolved_record()])
    manifest = json.loads(config.registration_manifest_path.read_text(encoding="utf-8"))
    if change == "revision":
        manifest["steps"]["sha256:" + "c" * 64] = manifest["steps"].pop(REVISION)
    elif change == "identity":
        manifest["steps"][REVISION]["stepIdentity"] = "another.identity"
    elif change == "entry":
        manifest["steps"][REVISION]["entryPoint"] = f"{__name__}:BarrierStep"
    elif change == "abi":
        manifest["steps"][REVISION]["abi"] = "syntara.container/v0"
    elif change == "schemas":
        manifest["steps"][REVISION]["schemaDigests"]["input"] = "sha256:" + "c" * 64
    else:
        manifest["steps"][REVISION]["unexpected"] = True
    config.registration_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    bindings = create_bindings()
    with pytest.raises(RuntimeServerError, match="registration manifest"):
        resolved_step_registry(config, bindings)


def test_untrusted_binding_is_rejected(tmp_path: Path) -> None:
    config = _resolved_config(tmp_path, [_resolved_record("platform-runner")])
    step = EchoStep()
    untrusted = StepBindings()
    untrusted.bind(StepDescriptor.for_step("smoke.echo.v1", step), step)
    with pytest.raises(RuntimeServerError, match="invalid installed binding"):
        resolved_step_registry(config, untrusted)


@pytest.mark.parametrize("kind", ["custom-runner", "platform-runner"])
def test_resolved_identity_cannot_fall_back_to_same_class(tmp_path: Path, kind: str) -> None:
    record = _resolved_record(kind) | {"stepIdentity": "another.identity"}
    config = _resolved_config(tmp_path, [record])
    if kind == "custom-runner":
        manifest = json.loads(config.registration_manifest_path.read_text(encoding="utf-8"))
        manifest["steps"][REVISION]["stepIdentity"] = "another.identity"
        config.registration_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    bindings = create_bindings()
    with pytest.raises(RuntimeServerError, match="no installed binding"):
        resolved_step_registry(config, bindings)


def test_shared_class_uses_the_selected_binding_schemas(tmp_path: Path) -> None:
    other_revision = "sha256:" + "c" * 64
    records = [
        _resolved_record("platform-runner"),
        _resolved_record("platform-runner") | {
            "stepRevisionId": other_revision,
            "stepIdentity": "another.identity",
            "schemaDigests": _schema_digests(
                {
                    "type": "object",
                    "properties": {"message": {"type": "string", "minLength": 6}},
                    "required": ["message"],
                }
            ),
        },
    ]
    bindings = create_bindings()
    other = installed_action_bindings(
        "another.identity",
        EchoStep(),
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string", "minLength": 6}},
            "required": ["message"],
        },
    ).binding("another.identity")
    bindings.bind(other.descriptor, other.step)
    registry = resolved_step_registry(_resolved_config(tmp_path, records), bindings)

    assert registry.resolve(REVISION)(_invocation()).output == {"message": "hello"}
    result = registry.resolve(other_revision)(
        replace(_invocation(), step_revision_id=other_revision)
    )
    assert result.error_result is not None
    assert result.error_result["code"] == "INVALID_INPUT"


def test_resolved_schema_digests_must_match_the_installed_binding(tmp_path: Path) -> None:
    record = _resolved_record("platform-runner")
    record["schemaDigests"] = {**_schema_digests(), "input": "sha256:" + "c" * 64}
    config = _resolved_config(tmp_path, [record])
    bindings = create_bindings()

    with pytest.raises(RuntimeServerError, match="schema digests differ"):
        resolved_step_registry(config, bindings)


@pytest.mark.parametrize("address", ["127.0.0.1:50051", "localhost:50051", "[::1]:50051"])
def test_tcp_runtime_listener_requires_mutual_tls(address: str, tmp_path: Path) -> None:
    config_path = write_runtime_config(tmp_path)
    with pytest.raises(RuntimeServerError, match="mutual TLS"):
        load_runtime_server_config({
            "SYNTARA_RUNTIME_CONFIG": str(config_path),
            "SYNTARA_RUNTIME_GRPC_ADDRESS": address,
        })


def test_direct_runtime_config_rejects_plaintext_tcp(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = replace(_resolved_config(tmp_path, [_resolved_record()]), address="127.0.0.1:50051")
        with pytest.raises(RuntimeServerError, match="mutual TLS"):
            create_runtime_server(config)
    asyncio.run(scenario())


def test_async_shutdown_emits_cancelled_event() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()

        def handler(invocation: ContainerInvocation) -> Any:
            started.set()
            release.wait(5)
            return None

        from syntara_plugin.runtime import StepRegistry
        service = AsyncContainerRuntimeService(StepRegistry({"example.step": handler}))
        invocation = replace(_invocation(), step_revision_id="example.step")
        from syntara_plugin.runtime.container import _request_from_invocation

        async def requests():
            yield container_pb2.ExecuteClientMessage(start=_request_from_invocation(invocation))
            await asyncio.Event().wait()

        async def consume():
            return [message async for message in service.Execute(requests(), FakeServicerContext())]

        task = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(started.wait, 2)
            service.shutdown()
            messages = await asyncio.wait_for(task, 2)
            assert len(messages) == 1
            assert messages[0].event.failure.code == FailureCode.CANCELLED
            assert messages[0].event.failure.outcome == "not_completed"
        finally:
            release.set()
    asyncio.run(scenario())


def test_serve_runtime_waits_for_grace_before_revoking_invocations(monkeypatch: Any) -> None:
    from syntara_plugin.runtime import server as runtime_server

    async def scenario() -> None:
        stopping = None
        stop_started = asyncio.Event()
        stop_finished = asyncio.Event()
        revoked = False

        class Server:
            async def start(self) -> None:
                pass

            async def stop(self, grace: float) -> None:
                assert grace == 30
                stop_started.set()
                await stop_finished.wait()

        class Service:
            def shutdown(self) -> None:
                nonlocal revoked
                revoked = True

        def add_signal_handler(_signal: Any, callback: Any) -> None:
            nonlocal stopping
            stopping = callback

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", add_signal_handler)
        monkeypatch.setattr(runtime_server, "_create_runtime_server", lambda _config: (Server(), Service()))
        task = asyncio.create_task(runtime_server.serve_runtime(None))
        await asyncio.sleep(0)
        assert stopping is not None
        stopping()
        await asyncio.wait_for(stop_started.wait(), 2)
        try:
            assert not revoked
        finally:
            stop_finished.set()
            await task
        assert revoked

    asyncio.run(scenario())
