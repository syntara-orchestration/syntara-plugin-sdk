"""Invocation registries are cleaned without deadlocks or per-invocation growth."""

from __future__ import annotations

import asyncio
import gc
import subprocess
import sys
import textwrap

import grpc
from syntara_plugin.runtime import (
    ContainerClient,
    ContainerInvocation,
    ContainerResult,
    StepRegistry,
)
from syntara_plugin.runtime import toolbox as toolbox_module
from syntara_plugin.runtime.credential_callback import CallbackCredentialProvider
from syntara_plugin.runtime.protocol import container_pb2_grpc
from syntara_plugin.runtime.server import AsyncContainerRuntimeService

# Runs in a subprocess: a deadlock must fail the test, not hang the suite.
_FINALIZER_CHURN = textwrap.dedent(
    """
    import gc
    import threading
    import time
    from threading import Barrier, Event

    from syntara_plugin.runtime import credential_callback as callbacks
    from syntara_plugin.runtime import toolbox as toolboxes
    from syntara_plugin.runtime.credential_callback import CallbackCredentialProvider


    def view(name):
        provider = CallbackCredentialProvider(name, lambda _: None, Event(), 5)
        return provider.restrict(frozenset(), lambda _: None)


    # Forced interleaving: one thread holds the toolbox registry lock and
    # cleans a callback view, while another view's finalizer cascades into
    # toolbox cleanup.
    first, second = view("forced-a"), view("forced-b")
    barrier = Barrier(2, timeout=5)

    def cascade(item):
        barrier.wait()
        del item

    def hold_and_clean(view_id):
        with toolboxes._states_lock:
            barrier.wait()
            time.sleep(0.1)
            callbacks._discard_callback_state(view_id)

    workers = [
        threading.Thread(target=cascade, args=(second,), daemon=True),
        threading.Thread(target=hold_and_clean, args=(id(first),), daemon=True),
    ]
    del second
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert not any(worker.is_alive() for worker in workers), "forced interleaving deadlocked"
    del first

    # Organic churn: concurrent invocations creating and dropping providers,
    # with cyclic garbage so collection runs finalizers on arbitrary threads.
    stop = Event()

    def churn(index):
        while not stop.is_set():
            item = view(f"churn-{index}")
            cycle = [item]
            cycle.append(cycle)
            del item, cycle
            if index == 0:
                gc.collect()

    threads = [threading.Thread(target=churn, args=(index,), daemon=True) for index in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(3)
    stop.set()
    for thread in threads:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads), "churn deadlocked"
    gc.collect()
    assert not toolboxes._states and not callbacks._callback_states
    print("ok")
    """
)


def test_registry_finalizers_do_not_deadlock() -> None:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _FINALIZER_CHURN],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError("registry cleanup deadlocked") from error
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip().endswith("ok")


def test_async_runtime_releases_each_invocation() -> None:
    step = "example.registry.cleanup.v1"

    def handler(invocation: ContainerInvocation) -> ContainerResult:
        provider = invocation.credential_provider
        assert isinstance(provider, CallbackCredentialProvider)
        provider._public_toolbox.log("INFO", "record")
        return ContainerResult("sha256:" + "a" * 64, output={})

    def providers() -> int:
        return sum(isinstance(item, CallbackCredentialProvider) for item in gc.get_objects())

    async def scenario() -> None:
        server = grpc.aio.server()
        service = AsyncContainerRuntimeService(StepRegistry({step: handler}))
        container_pb2_grpc.add_ContainerServiceServicer_to_server(service, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        try:
            client = ContainerClient(channel)
            gc.collect()
            baseline = (len(toolbox_module._states), providers())
            for index in range(5):
                await asyncio.to_thread(
                    client.execute, ContainerInvocation(f"cleanup-{index}", step, {}, 5)
                )
            await asyncio.sleep(0.3)
            gc.collect()
            assert (len(toolbox_module._states), providers()) == baseline
        finally:
            channel.close()
            service.shutdown()
            await server.stop(grace=0)

    asyncio.run(scenario())
