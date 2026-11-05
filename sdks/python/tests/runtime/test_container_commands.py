"""Tests for container-side commands handled by the Execution Plane."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import grpc
from syntara_plugin.runtime import (
    ContainerClient,
    ContainerInvocation,
    ContainerResult,
    ContainerToolbox,
    StepRegistry,
)
from syntara_plugin.runtime.container import add_container_runtime_service
from syntara_plugin.runtime.protocol import container_pb2
from syntara_plugin.runtime.server import AsyncContainerRuntimeService

OUTPUT_SCHEMA_DIGEST = "sha256:" + "a" * 64


class _ServicerContext:
    def add_done_callback(self, callback: Any) -> bool:
        del callback
        return True


class _RequestStream:
    def __init__(self) -> None:
        self._requests: asyncio.Queue[container_pb2.ExecuteStreamRequest] = asyncio.Queue()

    async def send(self, request: container_pb2.ExecuteStreamRequest) -> None:
        await self._requests.put(request)

    async def close(self) -> None:
        await self._requests.put(_sentinel_request())

    def __aiter__(self) -> AsyncIterator[container_pb2.ExecuteStreamRequest]:
        return self

    async def __anext__(self) -> container_pb2.ExecuteStreamRequest:
        request = await self._requests.get()
        if request.WhichOneof("request") is None:
            raise StopAsyncIteration
        return request


def _sentinel_request() -> container_pb2.ExecuteStreamRequest:
    return container_pb2.ExecuteStreamRequest()


def _execute_request() -> container_pb2.ExecuteStreamRequest:
    return container_pb2.ExecuteStreamRequest(
        execute=container_pb2.ExecuteRequest(
            invocation_id="invocation-1",
            workflow_run_id="workflow-run-1",
            step_revision_id="credential-resolution.v1",
            runtime_abi="syntara.container/v1alpha1",
            inputs_json=b"{}",
            credential_file_paths_json=b"{}",
            timeout_seconds=5,
            runtime_context_json=b"{}",
        )
    )


def test_credential_ref_resolution_round_trips_through_a_command() -> None:
    asyncio.run(_credential_ref_resolution_round_trips_through_a_command())


def test_client_resolves_credential_ref_commands_over_grpc() -> None:
    credential_ref = uuid4()
    resolved: list[str] = []

    def handler(
        invocation: ContainerInvocation, toolbox: ContainerToolbox
    ) -> ContainerResult:
        del invocation
        resolved.append(toolbox.resolve_credential_ref(credential_ref))
        return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"resolved": True})

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(
        server, StepRegistry({"credential-resolution.v1": handler})
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = ContainerClient(channel)
        result = client.execute(
            ContainerInvocation(
                invocation_id="invocation-2",
                workflow_run_id="workflow-run-2",
                step_revision_id="credential-resolution.v1",
                inputs={},
                credential_file_paths={},
                timeout_seconds=5,
            ),
            on_command=lambda command: (
                "resolved-secret"
                if command.credential_ref == credential_ref
                else ""
            ),
        )
    finally:
        channel.close()
        server.stop(grace=0).wait()

    assert result.output == {"resolved": True}
    assert resolved == ["resolved-secret"]


def test_credential_ref_command_errors_preserve_their_failure_code() -> None:
    asyncio.run(_credential_ref_command_errors_preserve_their_failure_code())


async def _credential_ref_command_errors_preserve_their_failure_code() -> None:
    def handler(
        invocation: ContainerInvocation, toolbox: ContainerToolbox
    ) -> ContainerResult:
        del invocation
        toolbox.resolve_credential_ref(uuid4())
        raise AssertionError("credential resolution should fail")

    service = AsyncContainerRuntimeService(
        StepRegistry({"credential-resolution.v1": handler})
    )
    requests = _RequestStream()
    await requests.send(_execute_request())
    events = service.Execute(requests, _ServicerContext())

    command_event = await anext(events)
    command = command_event.command
    await requests.send(
        container_pb2.ExecuteStreamRequest(
            command_response=container_pb2.ContainerCommandResponse(
                command_id=command.command_id,
                error=container_pb2.CommandError(
                    code="REMOTE_REJECTED",
                    message="The remote credential service rejected the request.",
                ),
            )
        )
    )
    failure_event = await anext(events)

    assert failure_event.WhichOneof("event") == "failure"
    assert failure_event.failure.code == "REMOTE_REJECTED"


async def _credential_ref_resolution_round_trips_through_a_command() -> None:
    credential_ref = uuid4()
    resolved: list[str] = []

    def handler(
        invocation: Any, toolbox: ContainerToolbox
    ) -> ContainerResult:
        del invocation
        resolved.append(toolbox.resolve_credential_ref(credential_ref))
        return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={"resolved": True})

    service = AsyncContainerRuntimeService(
        StepRegistry({"credential-resolution.v1": handler})
    )
    requests = _RequestStream()
    await requests.send(_execute_request())
    events = service.Execute(requests, _ServicerContext())

    command_event = await anext(events)
    assert command_event.WhichOneof("event") == "command"
    command = command_event.command
    assert command.WhichOneof("command") == "credential_ref_resolution"
    assert UUID(command.credential_ref_resolution.credential_ref) == credential_ref

    await requests.send(
        container_pb2.ExecuteStreamRequest(
            command_response=container_pb2.ContainerCommandResponse(
                command_id=command.command_id,
                credential_ref_resolution=container_pb2.CredentialRefResolutionResponse(
                    credential="resolved-secret"
                ),
            )
        )
    )
    result_event = await anext(events)
    assert result_event.WhichOneof("event") == "result", result_event
    assert result_event.result.output_json == b'{"resolved":true}'
    assert resolved == ["resolved-secret"]

    await requests.close()
