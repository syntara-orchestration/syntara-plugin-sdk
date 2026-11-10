"""Launch the in-container gRPC runtime from image-local step bindings."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import signal
import weakref
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import BoundedSemaphore, Event, Lock, Thread
from typing import Any

import grpc
from syntara_plugin.contracts import load_bundle, validate_document

from .abi import CONTAINER_ABI, ContainerInvocationError, FailureCode
from .adapter import container_step_for_binding
from .bindings import StepBinding, StepBindingError, StepBindings, validate_executable_step_binding
from .container import (
    INCOMPLETE_STEP_MESSAGE,
    ContainerInvocation,
    ContainerResult,
    ContainerStepHandler,
    StepRegistry,
    failure_event,
    invocation_from_request,
    terminal_execution_event,
)
from .credential_callback import CallbackCredentialProvider
from .execution import StepExecutionError
from .protocol import container_pb2, container_pb2_grpc
from .step import BaseStep

_RUNTIME_CONFIG = "SYNTARA_RUNTIME_CONFIG"
_RESOLVED_EXECUTION = "SYNTARA_RESOLVED_EXECUTION"
_REGISTRATION_MANIFEST = "SYNTARA_REGISTRATION_MANIFEST"
_DEFAULT_REGISTRATION_MANIFEST = Path("/opt/syntara/registration-manifest.json")
_GRPC_ADDRESS = "SYNTARA_RUNTIME_GRPC_ADDRESS"
_TLS_CERTIFICATE = "SYNTARA_RUNTIME_GRPC_TLS_CERT"
_TLS_KEY = "SYNTARA_RUNTIME_GRPC_TLS_KEY"
_TLS_CLIENT_CA = "SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA"
_DEFAULT_ADDRESS = "127.0.0.1:50051"
_DEFAULT_MAX_CONCURRENT_STEPS = 8
_CANCELLED_STEP_MESSAGE = "The step was cancelled."
_StepWorkItem = tuple[Future[ContainerResult], ContainerStepHandler, ContainerInvocation] | None


class _DaemonStepWorkerPool:
    """Run bounded synchronous step handlers on daemon threads."""

    def __init__(self, max_workers: int) -> None:
        self._work: Queue[_StepWorkItem] = Queue()
        self._shutdown_lock = Lock()
        self._shutdown = False
        self._sentinels_enqueued = False
        self._max_workers = max_workers
        self._threads: list[Thread] = []

    def submit(
        self, handler: ContainerStepHandler, invocation: ContainerInvocation,
    ) -> Future[ContainerResult]:
        future: Future[ContainerResult] = Future()
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new step work after shutdown")
            if not self._threads:
                self._threads = [
                    Thread(
                        target=self._worker,
                        args=(self._work, weakref.ref(self)),
                        name=f"syntara-step_{index}",
                        daemon=True,
                    )
                    for index in range(self._max_workers)
                ]
            # Keep the full list so a later submit can retry threads whose
            # start failed. Do not queue work until every worker has started.
            for thread in self._threads:
                if thread.ident is None:
                    thread.start()
            self._work.put((future, handler, invocation))
        return future

    def shutdown(self, *, wait: bool = False, cancel_futures: bool = False) -> None:
        with self._shutdown_lock:
            started_threads = self._begin_shutdown(cancel_futures)
        if wait:
            for thread in started_threads:
                thread.join()

    def _begin_shutdown(self, cancel_futures: bool) -> tuple[Thread, ...]:
        if self._shutdown:
            return tuple(thread for thread in self._threads if thread.ident is not None)
        self._shutdown = True
        if cancel_futures:
            self._cancel_queued_work()
        started_threads = tuple(thread for thread in self._threads if thread.ident is not None)
        if started_threads and not self._sentinels_enqueued:
            self._sentinels_enqueued = True
            for _thread in started_threads:
                self._work.put(None)
        return started_threads

    def _cancel_queued_work(self) -> None:
        while True:
            try:
                item = self._work.get_nowait()
            except Empty:
                return
            if item is not None:
                item[0].cancel()
            self._work.task_done()

    @staticmethod
    def _worker(
        work: Queue[_StepWorkItem],
        pool: weakref.ReferenceType[_DaemonStepWorkerPool],
    ) -> None:
        while True:
            try:
                item = work.get(timeout=1)
            except Empty:
                if pool() is None:
                    return
                continue
            if item is None:
                work.task_done()
                return
            try:
                future, handler, invocation = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = handler(invocation)
                except (KeyboardInterrupt, SystemExit) as error:
                    future.set_exception(error)
                    raise
                except Exception as error:
                    future.set_exception(error)
                else:
                    future.set_result(result)
            finally:
                work.task_done()
                del item, future, handler, invocation


class RuntimeServerError(RuntimeError):
    """Raised when the runtime adapter cannot start its gRPC server."""


@dataclass
class _ExecutionSession:
    """Mutable state owned by one async Execute request."""

    outgoing: asyncio.Queue[container_pb2.ExecuteServerMessage | None]
    invocation_id: str = ""
    cancellation: Event | None = None
    provider: CallbackCredentialProvider | None = None
    receiver: asyncio.Task[None] | None = None
    worker: asyncio.Task[None] | None = None


class AsyncContainerRuntimeService(container_pb2_grpc.ContainerServiceServicer):
    """Async invocation stream with bounded, service-owned step workers.

    Synchronous step code cannot be force-stopped when an invocation is
    cancelled. A worker slot remains occupied until that code returns, so
    timed-out or disconnected invocations cannot accumulate unbounded threads.
    """

    def __init__(
        self, steps: StepRegistry, *, max_concurrent_steps: int = _DEFAULT_MAX_CONCURRENT_STEPS,
    ) -> None:
        if not isinstance(max_concurrent_steps, int) or isinstance(max_concurrent_steps, bool):
            raise TypeError("max_concurrent_steps must be an integer")
        if max_concurrent_steps < 1:
            raise ValueError("max_concurrent_steps must be positive")
        self._steps = steps
        self._active: dict[str, tuple[Event, CallbackCredentialProvider, asyncio.Queue[Any]]] = {}
        self._step_executor = _DaemonStepWorkerPool(max_concurrent_steps)
        self._step_slots = BoundedSemaphore(max_concurrent_steps)
        self._shutdown = False

    async def Execute(
        self, requests: AsyncIterator[container_pb2.ExecuteClientMessage],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[container_pb2.ExecuteServerMessage]:
        session = _ExecutionSession(asyncio.Queue())
        try:
            await self._prepare_execution(requests, context, session)
            async for message in self._stream_messages(session.outgoing):
                yield message
        except StopAsyncIteration:
            return
        except ContainerInvocationError as error:
            yield container_pb2.ExecuteServerMessage(event=failure_event(session.invocation_id, error))
        finally:
            self._finish_execution(session)

    async def _prepare_execution(
        self,
        requests: AsyncIterator[container_pb2.ExecuteClientMessage],
        context: grpc.aio.ServicerContext,
        session: _ExecutionSession,
    ) -> None:
        first = await anext(requests)
        if first.WhichOneof("message") != "start":
            raise ContainerInvocationError(
                FailureCode.INVALID_INPUT,
                "The invocation is invalid.",
                outcome="not_started",
            )
        session.invocation_id = first.start.invocation_id
        invocation, cancellation = invocation_from_request(first.start)
        session.cancellation = cancellation
        handler = self._steps.resolve(invocation.step_revision_id)
        loop = asyncio.get_running_loop()

        def send(request: container_pb2.CredentialRequest) -> None:
            try:
                loop.call_soon_threadsafe(
                    session.outgoing.put_nowait,
                    container_pb2.ExecuteServerMessage(credential_request=request),
                )
            except RuntimeError:
                pass

        provider = CallbackCredentialProvider(
            session.invocation_id, send, cancellation, invocation.timeout_seconds
        )
        session.provider = provider
        object.__setattr__(invocation, "credential_provider", provider)
        self._register_invocation(
            session.invocation_id, cancellation, provider, session.outgoing
        )

        def close() -> None:
            invocation.cancellation.set()
            provider.close()

        context.add_done_callback(lambda _context: close())
        session.receiver = asyncio.create_task(
            self._receive_responses(requests, provider, invocation, session.outgoing)
        )
        session.worker = asyncio.create_task(
            self._run_step(handler, invocation, provider, session.outgoing)
        )

    @staticmethod
    async def _stream_messages(
        outgoing: asyncio.Queue[container_pb2.ExecuteServerMessage | None],
    ) -> AsyncIterator[container_pb2.ExecuteServerMessage]:
        while True:
            message = await outgoing.get()
            if message is None:
                return
            yield message

    def _finish_execution(self, session: _ExecutionSession) -> None:
        if session.cancellation is not None:
            session.cancellation.set()
            if session.provider is not None:
                session.provider.close()
            self._unregister_invocation(session.invocation_id, session.cancellation)
        for task in (session.receiver, session.worker):
            if task is not None:
                task.cancel()

    @staticmethod
    async def _receive_responses(
        requests: AsyncIterator[container_pb2.ExecuteClientMessage],
        provider: CallbackCredentialProvider,
        invocation: ContainerInvocation,
        outgoing: asyncio.Queue[container_pb2.ExecuteServerMessage | None],
    ) -> None:
        try:
            async for message in requests:
                if message.WhichOneof("message") != "credential_response":
                    invocation.cancellation.set()
                    outgoing.put_nowait(container_pb2.ExecuteServerMessage(event=failure_event(
                        invocation.invocation_id, ContainerInvocationError(
                            FailureCode.CANCELLED, _CANCELLED_STEP_MESSAGE, outcome="not_completed"
                        )
                    )))
                    outgoing.put_nowait(None)
                    return
                provider.receive(message.credential_response)
        finally:
            provider.close()

    async def _run_step(
        self,
        handler: ContainerStepHandler,
        invocation: ContainerInvocation,
        provider: CallbackCredentialProvider,
        outgoing: asyncio.Queue[container_pb2.ExecuteServerMessage | None],
    ) -> None:
        try:
            result = await self._execute_step(handler, invocation)
            event = terminal_execution_event(invocation, result)
        except StepExecutionError as error:
            event = failure_event(invocation.invocation_id, ContainerInvocationError(
                error.failure_code, error.safe_message, outcome=error.outcome
            ))
        except ContainerInvocationError as error:
            event = failure_event(invocation.invocation_id, error)
        except Exception:
            event = failure_event(invocation.invocation_id, ContainerInvocationError(
                FailureCode.OUTCOME_UNKNOWN, INCOMPLETE_STEP_MESSAGE, outcome="unknown"
            ))
        finally:
            provider.close()
        outgoing.put_nowait(container_pb2.ExecuteServerMessage(event=event))
        outgoing.put_nowait(None)

    async def Cancel(
        self, request: container_pb2.CancelRequest, context: grpc.aio.ServicerContext,
    ) -> container_pb2.CancelResponse:
        active = self._active.get(request.invocation_id)
        if active is None:
            return container_pb2.CancelResponse(accepted=False)
        cancellation, provider, outgoing = active
        cancellation.set()
        provider.close()
        outgoing.put_nowait(container_pb2.ExecuteServerMessage(event=failure_event(
            request.invocation_id, ContainerInvocationError(
                FailureCode.CANCELLED, _CANCELLED_STEP_MESSAGE, outcome="not_completed"
            )
        )))
        outgoing.put_nowait(None)
        return container_pb2.CancelResponse(accepted=True)

    def shutdown(self) -> None:
        """Revoke active reads and stop accepting step work."""
        if self._shutdown:
            return
        self._shutdown = True
        for invocation_id, (cancellation, provider, outgoing) in self._active.copy().items():
            cancellation.set()
            provider.close()
            outgoing.put_nowait(container_pb2.ExecuteServerMessage(event=failure_event(
                invocation_id, ContainerInvocationError(
                    FailureCode.CANCELLED, _CANCELLED_STEP_MESSAGE, outcome="not_completed"
                )
            )))
            outgoing.put_nowait(None)
        # Running Python code cannot be stopped safely. Daemon workers keep it
        # bounded without making interpreter shutdown wait for it to return.
        self._step_executor.shutdown(wait=False, cancel_futures=True)

    async def Health(
        self,
        request: container_pb2.HealthRequest,
        context: grpc.aio.ServicerContext,
    ) -> container_pb2.HealthResponse:
        return container_pb2.HealthResponse(
            ready=True,
            runtime_abi=CONTAINER_ABI,
            step_revision_ids=self._steps.step_revision_ids,
        )

    def _register_invocation(self, invocation_id: str, cancellation: Event, provider: CallbackCredentialProvider, outgoing: asyncio.Queue[Any]) -> None:
        if invocation_id in self._active:
            raise ContainerInvocationError(
                FailureCode.CONFLICT,
                "The step is already running.",
                outcome="not_started",
            )
        self._active[invocation_id] = (cancellation, provider, outgoing)

    def _unregister_invocation(self, invocation_id: str, cancellation: Event) -> None:
        active = self._active.get(invocation_id)
        if active is not None and active[0] is cancellation:
            del self._active[invocation_id]

    async def _execute_step(
        self, handler: ContainerStepHandler, invocation: ContainerInvocation
    ) -> ContainerResult:
        if self._shutdown:
            raise ContainerInvocationError(
                FailureCode.CANCELLED, _CANCELLED_STEP_MESSAGE, outcome="not_started"
            )
        if not self._step_slots.acquire(blocking=False):
            raise ContainerInvocationError(
                FailureCode.RATE_LIMITED,
                "The runtime is at capacity.",
                retryable=True,
                outcome="not_started",
            )
        try:
            worker = self._step_executor.submit(handler, invocation)
        except Exception:
            self._step_slots.release()
            raise
        worker.add_done_callback(self._release_step_slot)
        task = asyncio.wrap_future(worker)
        task.add_done_callback(_discard_step_result)
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=invocation.timeout_seconds
            )
        except TimeoutError:
            if task.done():
                return task.result()
            invocation.cancellation.set()
            raise ContainerInvocationError(
                FailureCode.TIMEOUT,
                "The step exceeded its execution timeout.",
                retryable=True,
                outcome="not_completed",
            ) from None
        except asyncio.CancelledError:
            invocation.cancellation.set()
            raise


    def _release_step_slot(self, worker: Future[ContainerResult]) -> None:
        del worker
        self._step_slots.release()


def _discard_step_result(task: asyncio.Future[ContainerResult]) -> None:
    if not task.cancelled():
        task.exception()


@dataclass(frozen=True)
class RuntimeServerConfig:
    """Validated, non-secret inputs needed to launch one runtime server."""

    bindings_factory: str
    address: str = _DEFAULT_ADDRESS
    tls_certificate: Path | None = None
    tls_key: Path | None = None
    tls_client_ca: Path | None = None
    resolved_execution_path: Path | None = None
    registration_manifest_path: Path = _DEFAULT_REGISTRATION_MANIFEST


@dataclass(frozen=True)
class _ResolvedExecutionBinding:
    """One validated, platform-supplied container dispatch binding."""

    revision: str
    step_identity: str
    runtime_kind: str
    entry_point: str
    abi: str
    schema_digests: Mapping[str, str]


def load_runtime_server_config(
    environ: Mapping[str, str] = os.environ,
    *,
    config_path: Path | None = None,
) -> RuntimeServerConfig:
    """Read the language-neutral runtime config and this adapter's settings."""

    document = _runtime_document(environ, config_path)
    bindings_factory = _bindings_factory(document)
    address, certificate, key, client_ca = _listener_settings(environ)
    resolved_path = environ.get(_RESOLVED_EXECUTION, "").strip()
    manifest_path = environ.get(_REGISTRATION_MANIFEST, "").strip()
    return RuntimeServerConfig(
        bindings_factory,
        address,
        certificate,
        key,
        client_ca,
        Path(resolved_path) if resolved_path else None,
        Path(manifest_path) if manifest_path else _DEFAULT_REGISTRATION_MANIFEST,
    )


def load_runtime_bindings(specification: str) -> StepBindings:
    """Load one image-owned ``StepBindings`` factory.

    The startup error stays bounded and free of raw factory exception text,
    which may contain credentials, URLs, or filesystem paths; the original
    exception is preserved as the cause for the traceback.
    """

    module_name, separator, attribute_name = specification.rpartition(":")
    if not separator or not module_name or not attribute_name:
        message = "runtime bindings factory must use module:factory syntax"
        raise RuntimeServerError(message)
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute_name)
        bindings = factory()
    except Exception as error:
        message = "the configured runtime bindings factory could not be loaded"
        raise RuntimeServerError(message) from error
    if not isinstance(bindings, StepBindings):
        message = "the runtime bindings factory must return StepBindings"
        raise RuntimeServerError(message)
    if not bindings.step_identities:
        message = "the runtime bindings factory must register at least one step type"
        raise RuntimeServerError(message)
    return bindings


def create_runtime_server(config: RuntimeServerConfig) -> grpc.aio.Server:
    """Create and bind, but do not start, the in-container gRPC server."""
    return _create_runtime_server(config)[0]


def _create_runtime_server(config: RuntimeServerConfig) -> tuple[grpc.aio.Server, AsyncContainerRuntimeService]:

    _validate_listener(config.address, config.tls_certificate, config.tls_key, config.tls_client_ca)
    bindings = load_runtime_bindings(config.bindings_factory)
    steps = resolved_step_registry(config, bindings)
    server = grpc.aio.server()
    service = AsyncContainerRuntimeService(steps)
    container_pb2_grpc.add_ContainerServiceServicer_to_server(service, server)
    credentials = _server_credentials(config)
    port = (
        server.add_secure_port(config.address, credentials)
        if credentials is not None
        else server.add_insecure_port(config.address)
    )
    if port == 0:
        message = f"could not bind the runtime gRPC listener: {config.address}"
        raise RuntimeServerError(message)
    return server, service


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _read_json_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, ValueError, RecursionError) as error:
        raise RuntimeServerError(f"could not read valid {label}: {path}") from error
    if not isinstance(document, Mapping):
        raise RuntimeServerError(f"{label} must be a JSON object")
    return document


def _registration_steps(path: Path) -> Mapping[str, Any]:
    manifest = _read_json_object(path, "registration manifest")
    if validate_document(load_bundle(), "registration_manifest", manifest):
        raise RuntimeServerError("registration manifest violates its contract")
    steps = manifest["steps"]
    assert isinstance(steps, Mapping)
    return steps


def _resolved_execution_bindings(config: RuntimeServerConfig) -> list[_ResolvedExecutionBinding]:
    if config.resolved_execution_path is None:
        raise RuntimeServerError(f"{_RESOLVED_EXECUTION} must name a resolved execution file")
    document = _read_json_object(config.resolved_execution_path, "resolved execution data")
    if validate_document(load_bundle(), "resolved_execution", document):
        raise RuntimeServerError("resolved execution data violates its contract")
    records = document["bindings"]
    assert isinstance(records, list)
    return [_parse_resolved_execution_binding(record) for record in records]


def _parse_resolved_execution_binding(record: Any) -> _ResolvedExecutionBinding:
    assert isinstance(record, Mapping)
    revision = record["stepRevisionId"]
    step_identity = record["stepIdentity"]
    runtime_kind = record["runtimeKind"]
    entry_point = record["entryPoint"]
    abi = record["abi"]
    schema_digests = record["schemaDigests"]
    assert isinstance(revision, str)
    assert isinstance(step_identity, str)
    assert isinstance(runtime_kind, str)
    assert isinstance(entry_point, str)
    assert isinstance(abi, str)
    assert isinstance(schema_digests, Mapping)
    assert all(isinstance(name, str) and isinstance(digest, str) for name, digest in schema_digests.items())
    return _ResolvedExecutionBinding(
        revision, step_identity, runtime_kind, entry_point, abi, schema_digests
    )


def _validate_custom_runner_mapping(
    binding: _ResolvedExecutionBinding,
    manifest_steps: Mapping[str, Any] | None,
    manifest_path: Path,
) -> Mapping[str, Any] | None:
    if binding.runtime_kind != "custom-runner":
        return manifest_steps
    steps = manifest_steps or _registration_steps(manifest_path)
    expected = {
        "stepIdentity": binding.step_identity,
        "entryPoint": binding.entry_point,
        "abi": binding.abi,
        "schemaDigests": binding.schema_digests,
    }
    if steps.get(binding.revision) != expected:
        raise RuntimeServerError("resolved custom runner does not match registration manifest")
    return steps


def _load_typed_step(entry_point: str) -> type[BaseStep[Any, Any]]:
    module_name, _, class_name = entry_point.partition(":")
    try:
        implementation = getattr(importlib.import_module(module_name), class_name)
    except Exception as error:
        raise RuntimeServerError("resolved entry point could not be loaded") from error
    if not isinstance(implementation, type) or not issubclass(implementation, BaseStep):
        raise RuntimeServerError("resolved entry point must name a step type class")
    return implementation


def _installed_binding_for_step(
    bindings: StepBindings,
    step_identity: str,
    implementation: type[BaseStep[Any, Any]],
) -> StepBinding:
    try:
        installed_binding = bindings.binding(step_identity)
    except StepBindingError as error:
        raise RuntimeServerError("resolved step type identity has no installed binding") from error
    if type(installed_binding.step) is not implementation:
        raise RuntimeServerError("resolved entry point does not match the installed binding")
    return installed_binding


def _container_handler_for_resolved_binding(
    binding: _ResolvedExecutionBinding,
    installed_binding: StepBinding,
) -> ContainerStepHandler:
    try:
        validate_executable_step_binding(installed_binding.descriptor, installed_binding.step)
        if installed_binding.descriptor.installed_entry_point != binding.entry_point:
            raise StepBindingError("resolved entry point differs from the installed contract")
        for name, digest in binding.schema_digests.items():
            if installed_binding.descriptor.schema_digest(name) != digest:
                raise StepBindingError("resolved schema digests differ from the installed contract")
        return container_step_for_binding(installed_binding, step_revision_id=binding.revision)
    except (TypeError, ValueError) as error:
        raise RuntimeServerError(f"resolved entry point has an invalid installed binding: {error}") from error


def resolved_step_registry(config: RuntimeServerConfig, bindings: StepBindings) -> StepRegistry:
    """Bind platform-supplied revisions to executable image-local bindings.

    The caller supplies a trusted read-only file. The image registration
    manifest checks custom-runner mappings; it is neither schema provenance nor
    proof of image identity. The existing installed binding supplies schemas.
    """
    handlers: dict[str, ContainerStepHandler] = {}
    manifest_steps: Mapping[str, Any] | None = None
    for binding in _resolved_execution_bindings(config):
        if binding.revision in handlers:
            raise RuntimeServerError("resolved execution data contains a duplicate step type revision")
        manifest_steps = _validate_custom_runner_mapping(
            binding, manifest_steps, config.registration_manifest_path
        )
        implementation = _load_typed_step(binding.entry_point)
        installed_binding = _installed_binding_for_step(
            bindings, binding.step_identity, implementation
        )
        handlers[binding.revision] = _container_handler_for_resolved_binding(
            binding, installed_binding
        )
    return StepRegistry(handlers)


async def serve_runtime(config: RuntimeServerConfig) -> None:
    """Run the gRPC server until SIGTERM or SIGINT is received."""

    server, service = _create_runtime_server(config)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    await server.start()
    try:
        await stopping.wait()
    finally:
        try:
            await server.stop(grace=30)
        finally:
            service.shutdown()


def main() -> None:
    """Console entrypoint used by a final Python workload image."""

    asyncio.run(serve_runtime(load_runtime_server_config()))


def _runtime_document(
    environ: Mapping[str, str],
    config_path: Path | None,
) -> Mapping[str, Any]:
    path = config_path
    if path is None:
        raw_path = environ.get(_RUNTIME_CONFIG, "").strip()
        if not raw_path:
            message = f"{_RUNTIME_CONFIG} must name a runtime config JSON file"
            raise RuntimeServerError(message)
        path = Path(raw_path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        message = f"could not read a valid runtime config: {path}"
        raise RuntimeServerError(message) from error
    if not isinstance(document, Mapping):
        message = "runtime config must be a JSON object"
        raise RuntimeServerError(message)
    return document


def _bindings_factory(document: Mapping[str, Any]) -> str:
    if set(document) != {"abi", "adapter"}:
        message = "runtime config requires exactly abi and adapter"
        raise RuntimeServerError(message)
    if document["abi"] != CONTAINER_ABI:
        message = f"runtime config ABI must be {CONTAINER_ABI}"
        raise RuntimeServerError(message)
    adapter = document["adapter"]
    if not isinstance(adapter, Mapping) or set(adapter) != {"bindingsFactory"}:
        message = "the Python runtime adapter requires exactly bindingsFactory"
        raise RuntimeServerError(message)
    bindings_factory = adapter["bindingsFactory"]
    if not isinstance(bindings_factory, str) or not bindings_factory.strip():
        message = "adapter bindingsFactory must name a module:factory function"
        raise RuntimeServerError(message)
    return bindings_factory.strip()


def _listener_settings(
    environ: Mapping[str, str],
) -> tuple[str, Path | None, Path | None, Path | None]:
    address = environ.get(_GRPC_ADDRESS, _DEFAULT_ADDRESS).strip()
    certificate = _optional_path(environ.get(_TLS_CERTIFICATE))
    key = _optional_path(environ.get(_TLS_KEY))
    client_ca = _optional_path(environ.get(_TLS_CLIENT_CA))
    _validate_listener(address, certificate, key, client_ca)
    return address, certificate, key, client_ca


def _validate_listener(
    address: str, certificate: Path | None, key: Path | None, client_ca: Path | None,
) -> None:
    host, port = _split_address(address)
    if not host or not port.isdecimal() or not 1 <= int(port) <= 65_535:
        raise RuntimeServerError(f"{_GRPC_ADDRESS} must be a host:port TCP address")
    supplied_tls = (certificate, key, client_ca)
    if any(value is not None for value in supplied_tls) and not all(
        value is not None for value in supplied_tls
    ):
        raise RuntimeServerError("runtime gRPC TLS requires certificate, key, and client CA paths")
    if certificate is None:
        raise RuntimeServerError("runtime gRPC TCP listeners require mutual TLS")


def _split_address(address: str) -> tuple[str, str]:
    if "://" in address:
        return "", ""
    if address.startswith("[") and address.count("]") == 1:
        host, port = address.rsplit(":", 1)
        return host.removeprefix("[").removesuffix("]"), port
    if address.count(":") == 1:
        return address.split(":", 1)
    return "", ""


def _optional_path(value: str | None) -> Path | None:
    if value is None or not value.strip():
        return None
    return Path(value).expanduser()


def _server_credentials(config: RuntimeServerConfig) -> grpc.ServerCredentials | None:
    if config.tls_certificate is None:
        return None
    assert config.tls_key is not None
    assert config.tls_client_ca is not None
    try:
        certificate = config.tls_certificate.read_bytes()
        key = config.tls_key.read_bytes()
        client_ca = config.tls_client_ca.read_bytes()
    except OSError as error:
        message = "could not read the runtime gRPC TLS material"
        raise RuntimeServerError(message) from error
    return grpc.ssl_server_credentials(
        [(key, certificate)],
        root_certificates=client_ca,
        require_client_auth=True,
    )


if __name__ == "__main__":
    main()
