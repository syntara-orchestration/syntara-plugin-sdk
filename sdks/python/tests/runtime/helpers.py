"""Shared helpers for container runtime protocol tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

from syntara_plugin.runtime.protocol import container_pb2


def stream_request(
    request: container_pb2.ExecuteRequest,
) -> container_pb2.ExecuteStreamRequest:
    return container_pb2.ExecuteStreamRequest(execute=request)


async def async_stream_request(
    request: container_pb2.ExecuteRequest,
) -> AsyncIterator[container_pb2.ExecuteStreamRequest]:
    yield stream_request(request)
