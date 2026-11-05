"""SIGTERM shutdown wiring installed by ``add_container_runtime_service``."""

from __future__ import annotations

import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import grpc
from syntara_plugin.runtime import ContainerResult, StepRegistry, add_container_runtime_service

OUTPUT_SCHEMA_DIGEST = "sha256:" + "a" * 64


def _server_with_service(**kwargs) -> grpc.Server:
    def succeed(_) -> ContainerResult:
        return ContainerResult(OUTPUT_SCHEMA_DIGEST, output={})

    server = grpc.server(ThreadPoolExecutor(max_workers=1))
    add_container_runtime_service(server, StepRegistry({"step.v1": succeed}), **kwargs)
    return server


def test_pid1_service_installation_registers_sigterm_handler(monkeypatch) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    try:
        # Other tests (serve_runtime) may have installed a handler already;
        # reset to the default so the installation path is deterministic.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        monkeypatch.setattr(os, "getpid", lambda: 1)
        server = _server_with_service()
        assert callable(signal.getsignal(signal.SIGTERM))
    finally:
        signal.signal(signal.SIGTERM, previous)
        server.stop(0)


def test_non_pid1_installation_is_skipped(monkeypatch) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        monkeypatch.setattr(os, "getpid", lambda: 12345)
        _server_with_service()
        # A host process (test harness, CLI run) must keep the default
        # disposition: installing a global handler there would hijack the
        # host's own SIGTERM policy.
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_explicit_opt_out_skips_installation(monkeypatch) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        monkeypatch.setattr(os, "getpid", lambda: 1)
        _server_with_service(install_sigterm_shutdown=False)
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_existing_sigterm_handler_is_preserved(monkeypatch) -> None:
    previous = signal.getsignal(signal.SIGTERM)

    def owned(_signum, _frame) -> None:
        return None

    signal.signal(signal.SIGTERM, owned)
    try:
        monkeypatch.setattr(os, "getpid", lambda: 1)
        _server_with_service()
        assert signal.getsignal(signal.SIGTERM) is owned
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_sigterm_stops_the_running_pid1_server(monkeypatch) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    stopped = Event()
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        monkeypatch.setattr(os, "getpid", lambda: 1)
        server = _server_with_service()
        server.add_insecure_port("127.0.0.1:0")
        server.start()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)

        waiter = threading.Thread(target=lambda: (server.wait_for_termination(), stopped.set()))
        waiter.start()
        handler(signal.SIGTERM, None)
        assert stopped.wait(timeout=10), "server did not stop after the SIGTERM handler ran"
        waiter.join(timeout=10)
    finally:
        signal.signal(signal.SIGTERM, previous)
