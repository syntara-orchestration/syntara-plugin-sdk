"""Exercise execution timeouts over real gRPC, including result delivery."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import grpc
import pytest
from syntara_plugin.runtime import (
    ContainerClient,
    ContainerInvocation,
    ContainerRemoteError,
    ContainerResult,
    FailureCode,
    StepRegistry,
    add_container_runtime_service,
)
from syntara_plugin.runtime.protocol import container_pb2_grpc
from syntara_plugin.runtime.server import AsyncContainerRuntimeService


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("transport_timeout", [None, 3.0])
@pytest.mark.parametrize("cooperative", [False, True])
def test_execution_timeout_arrives_as_workload_failure(
    asynchronous: bool, transport_timeout: float | None, cooperative: bool,
) -> None:
    """Expiry must deliver TIMEOUT instead of racing the transport deadline."""
    release, finished = Event(), Event()

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        try:
            if cooperative:
                invocation.cancellation.wait(5)
            else:
                release.wait(5)
            return ContainerResult("sha256:" + "a" * 64, output={"late": True})
        finally:
            finished.set()

    async def scenario() -> None:
        registry = StepRegistry({"example.wait.v1": handler})
        with ThreadPoolExecutor(max_workers=4) as pool:
            if asynchronous:
                server = grpc.aio.server()
                service = AsyncContainerRuntimeService(registry)
                container_pb2_grpc.add_ContainerServiceServicer_to_server(service, server)
            else:
                server = grpc.server(pool)
                service = add_container_runtime_service(server, registry)
            port = server.add_insecure_port("127.0.0.1:0")
            if asynchronous:
                await server.start()
            else:
                server.start()
            channel = grpc.insecure_channel(f"127.0.0.1:{port}")
            invocation = ContainerInvocation(
                invocation_id="timeout-1", workflow_run_id="run-1",
                step_revision_id="example.wait.v1", inputs={}, timeout_seconds=1,
            )
            try:
                with pytest.raises(ContainerRemoteError) as raised:
                    await asyncio.wait_for(asyncio.to_thread(
                        ContainerClient(channel).execute, invocation,
                        timeout_seconds=transport_timeout,
                    ), timeout=4)
                assert raised.value.code is FailureCode.TIMEOUT
                assert raised.value.source == "workload"
                assert raised.value.outcome == "not_completed"
                if cooperative:
                    assert await asyncio.to_thread(finished.wait, 1)
                else:
                    assert not finished.is_set()
            finally:
                release.set()
                channel.close()
                if asynchronous:
                    await server.stop(grace=0)
                else:
                    server.stop(grace=0).wait()
                service.shutdown()
                assert await asyncio.to_thread(finished.wait, 2)

    asyncio.run(scenario())


@pytest.mark.parametrize("transport_timeout", [None, 0.1, 60.0])
def test_unresponsive_server_keeps_a_bounded_unknown_outcome(
    transport_timeout: float | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delivery grace must not remove the RPC backstop or hide uncertainty."""
    from syntara_plugin.runtime import container as runtime_container

    monkeypatch.setattr(runtime_container, "_RESULT_DELIVERY_GRACE_SECONDS", 0.2)
    disconnected = Event()
    wire_deadlines: list[float] = []

    class UnresponsiveService(container_pb2_grpc.ContainerServiceServicer):
        def Execute(self, requests, context):
            next(requests)
            wire_deadlines.append(context.time_remaining())
            context.add_callback(disconnected.set)
            disconnected.wait(4)
            return iter(())

    with ThreadPoolExecutor(max_workers=2) as pool:
        server = grpc.server(pool)
        container_pb2_grpc.add_ContainerServiceServicer_to_server(UnresponsiveService(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        invocation = ContainerInvocation(
            invocation_id="timeout-1", workflow_run_id="run-1",
            step_revision_id="example.wait.v1", inputs={}, timeout_seconds=1,
        )
        try:
            started = monotonic()
            with pytest.raises(ContainerRemoteError) as raised:
                ContainerClient(channel).execute(invocation, timeout_seconds=transport_timeout)
            assert monotonic() - started < 3
            assert raised.value.code is FailureCode.TIMEOUT
            assert raised.value.source == "transport"
            assert raised.value.outcome == "unknown"
            # Even a caller-supplied long deadline cannot extend the backstop.
            assert len(wire_deadlines) == 1
            if transport_timeout == 0.1:
                assert 0 < wire_deadlines[0] < 0.2
            else:
                assert 0 < wire_deadlines[0] <= 1.3
        finally:
            disconnected.set()
            channel.close()
            server.stop(grace=0).wait()
