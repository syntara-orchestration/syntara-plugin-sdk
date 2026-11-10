"""The generic container runtime API for Syntara plugin workloads.

One container image may register many immutable step type revisions.  The Execution
Plane selects the revision in an :class:`ContainerInvocation`; the runtime
dispatches to the matching Python callable. Credential responses use a
separate sensitive message on an authenticated, confidential stream.
"""

from __future__ import annotations

import json
import os
import re
import signal
from _thread import LockType
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from queue import Queue
from threading import BoundedSemaphore, Event, Lock, Thread, current_thread
from time import monotonic
from typing import Any, Literal, NoReturn

import grpc
from jsonschema import Draft202012Validator, FormatChecker
from syntara_plugin.contracts import canonical_json_bytes, canonical_json_digest

from .abi import _PROVIDER_CODE, CONTAINER_ABI, ContainerInvocationError, FailureCode
from .credential_callback import CallbackCredentialProvider
from .credentials import CredentialAccessError, CredentialProvider
from .execution import StepExecutionError
from .outcome import (
    MAX_MESSAGE_LENGTH,
    ErrorResult,
    StandardOutputWrapper,
    error_result_base_schema,
    error_result_schema,
    freeze_json,
    validate_json_object,
)
from .protocol import container_pb2, container_pb2_grpc

_IDENTITY = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_OUTPUT_SCHEMA_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_MAX_JSON_BYTES = 1_048_576
_MAX_TIMEOUT_SECONDS = 86_400
_MAX_STATUS_CODE = 4_294_967_295
_MAX_SYNC_INVOCATIONS = 16
_SYNC_RUNTIME_WORKER_SLOTS = BoundedSemaphore(_MAX_SYNC_INVOCATIONS)
_PROGRESS_STATES = frozenset({"started", "heartbeat", "progress"})
INCOMPLETE_STEP_MESSAGE = "The step did not complete."
_CANCELLED_STEP_MESSAGE = "The step was cancelled."

# A platform-owned runner can handle any installed step type that the control
# plane has already resolved to that runner. It is a health-advertisement
# capability only, never a valid ``step_revision_id`` in an Execute request.
ANY_STEP_REVISION = "*"


def _thaw_json_document(value: Any) -> Any:
    """Copy immutable schema and result containers into plain JSON values."""

    if isinstance(value, Mapping):
        return {key: _thaw_json_document(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json_document(item) for item in value]
    return value


@dataclass(frozen=True)
class ContainerInvocation:
    """The selected type and the safe data supplied to it."""

    invocation_id: str
    workflow_run_id: str
    step_revision_id: str
    inputs: Mapping[str, Any]
    timeout_seconds: int
    runtime_context: Mapping[str, Any] = field(default_factory=dict)
    cancellation: Event = field(default_factory=Event, compare=False, repr=False)
    credential_provider: CredentialProvider | None = field(default=None, compare=False, repr=False)

    @property
    def cancelled(self) -> bool:
        """Whether the Execution Plane has requested cooperative cancellation."""

        return self.cancellation.is_set()


@dataclass(frozen=True)
class CredentialRequestContext:
    """Invocation-scoped information supplied to one host credential resolver.

    ``deadline`` is an absolute timestamp from :func:`time.monotonic`. Resolver
    code should use ``remaining_seconds`` for finite downstream timeouts and
    check ``cancellation`` while waiting on cancellable work.
    """

    invocation_id: str
    name: str
    cancellation: Event = field(compare=False, repr=False)
    deadline: float

    @property
    def cancelled(self) -> bool:
        """Whether the invocation ended or its deadline has elapsed."""

        return self.cancellation.is_set() or self.remaining_seconds <= 0

    @property
    def remaining_seconds(self) -> float:
        """Seconds remaining before the invocation deadline, clamped at zero."""

        return max(0.0, self.deadline - monotonic())


CredentialResolver = Callable[[CredentialRequestContext], Mapping[str, Any]]


@dataclass(frozen=True)
class ContainerProgress:
    """A non-terminal, user-safe update emitted by a step."""

    invocation_id: str
    sequence: int
    state: str
    message: str = ""
    percent: float | None = None


@dataclass(frozen=True)
class ContainerResult:
    """The terminal SDK output envelope of one step type invocation.

    This is the transport mapping for a valid ``StandardOutputWrapper``:
    status, status message, optional business output, and optional error
    metadata remain one ``Result`` event, including when the status is
    nonzero. A container ``Failure`` event is reserved for an invocation that
    cannot produce a valid SDK outcome. Output may be inline or represented by
    an object-store reference; that choice does not change the outcome.

    A step type returns either an inline JSON object or an object-store reference for
    its business result, never both. ``status_code`` zero is success; a nonzero
    value is a deliberate step type failure that remains a valid outcome and must
    carry the complete ``error_result`` and its effective ``error_schema_digest``.
    The schema digests let the control plane interpret the result without
    importing plugin Python code.
    """

    output_schema_digest: str
    output: Mapping[str, Any] | None = None
    object_reference: str | None = None
    status_code: int = 0
    status_message: str = ""
    error_result: Mapping[str, Any] | None = None
    error_schema_digest: str = ""

    def __post_init__(self) -> None:
        self._validate_schema_and_status_code()
        self._validate_output_payload()
        self._validate_status_metadata()

    def _validate_schema_and_status_code(self) -> None:
        if not _OUTPUT_SCHEMA_DIGEST.fullmatch(self.output_schema_digest):
            raise ValueError("output_schema_digest must be a sha256 digest")
        if not isinstance(self.status_code, int) or isinstance(self.status_code, bool):
            raise TypeError("status_code must be an integer")
        if not 0 <= self.status_code <= _MAX_STATUS_CODE:
            raise ValueError("status_code must be a uint32 value")

    def _validate_output_payload(self) -> None:
        has_output = self.output is not None
        has_reference = self.object_reference is not None
        if has_output and has_reference:
            raise ValueError("output and object_reference are mutually exclusive")
        if self.status_code == 0 and not has_output and not has_reference:
            raise ValueError("a successful result requires output or object_reference")
        if has_output:
            try:
                validate_json_object(self.output)
            except (TypeError, ValueError):
                raise ValueError("output must be a JSON object") from None
        if has_reference and (
            not isinstance(self.object_reference, str)
            or not _IDENTITY.fullmatch(self.object_reference)
        ):
            raise ValueError("object_reference must be a Syntara identity")

    def _validate_status_metadata(self) -> None:
        self._validate_status_message()
        if self.status_code == 0:
            self._validate_success_status_metadata()
        else:
            self._validate_failure_status_metadata()

    def _validate_status_message(self) -> None:
        if not isinstance(self.status_message, str) or len(self.status_message) > MAX_MESSAGE_LENGTH:
            raise ValueError(
                f"status message must contain at most {MAX_MESSAGE_LENGTH} characters"
            )

    def _validate_success_status_metadata(self) -> None:
        if self.error_result is not None or self.error_schema_digest:
            raise ValueError("a successful result must not carry error metadata")

    def _validate_failure_status_metadata(self) -> None:
        if self.error_result is None or not _OUTPUT_SCHEMA_DIGEST.fullmatch(
            self.error_schema_digest
        ):
            raise ValueError("a failed result requires an error result and schema digest")
        validate_json_object(self.error_result)
        document = _thaw_json_document(self.error_result)
        if not Draft202012Validator(error_result_base_schema()).is_valid(document):
            raise ValueError("error result must satisfy the SDK base schema")
        object.__setattr__(self, "error_result", freeze_json(document))


def _trusted_output_schema(output_schema: Mapping[str, Any]) -> tuple[Any, str]:
    """Validate the trusted output schema and return it with its digest."""

    try:
        schema_document = _thaw_json_document(output_schema)
        schema_digest = canonical_json_digest(schema_document)
        Draft202012Validator.check_schema(schema_document)
    except Exception:
        raise ValueError("the trusted output schema is invalid") from None
    return schema_document, schema_digest


def _resolve_container_output(
    result: ContainerResult,
    resolve_object_reference: Callable[[str], Mapping[str, Any]] | None,
) -> Mapping[str, Any] | None:
    """Resolve an opaque business-result reference when the result uses one."""

    if result.object_reference is None:
        return result.output
    if resolve_object_reference is None:
        raise ValueError("an object reference resolver is required for this result")
    try:
        output = resolve_object_reference(result.object_reference)
    except Exception:
        raise ValueError("the referenced business result could not be resolved") from None
    if output is None:
        raise _protocol_error()
    return output


def _validate_container_output(
    output: Mapping[str, Any] | None,
    schema_document: Mapping[str, Any],
) -> None:
    """Validate inline or resolved business output against the trusted schema."""

    if output is None:
        return
    try:
        validate_json_object(output)
        validator = Draft202012Validator(schema_document, format_checker=FormatChecker())
        is_valid = validator.is_valid(_thaw_json_document(output))
    except Exception:
        raise _protocol_error() from None
    if not is_valid:
        raise _protocol_error()


def _error_from_container_result(
    result: ContainerResult,
    error_schema: Mapping[str, Any] | None,
) -> ErrorResult | None:
    """Validate and reconstruct deliberate step type-failure metadata."""

    try:
        if result.status_code == 0:
            return None
        error_document = _thaw_json_document(result.error_result)
        candidate_schemas = []
        if error_schema is not None:
            candidate_schemas.append(error_schema)
        candidate_schemas.append(error_result_schema())
        candidate_schemas.append(error_result_base_schema())
        for candidate in candidate_schemas:
            schema = _thaw_json_document(candidate)
            Draft202012Validator.check_schema(schema)
            if result.error_schema_digest != canonical_json_digest(schema):
                continue
            if not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(
                error_document
            ):
                raise ValueError("invalid error result")
            return ErrorResult.model_validate(error_document)
        raise ValueError("error schema digest mismatch")
    except Exception:
        raise _protocol_error() from None


def standard_output_from_container_result(
    result: ContainerResult,
    *,
    output_schema: Mapping[str, Any],
    error_schema: Mapping[str, Any] | None = None,
    resolve_object_reference: Callable[[str], Mapping[str, Any]] | None = None,
) -> StandardOutputWrapper:
    """Reconstruct a wrapper, resolving opaque host-owned references when present.

    ``output_schema`` is the caller's trusted schema from the invocation
    contract. Its canonical digest must match the result, and the business
    output is validated against it before a wrapper is created. ``error_schema``
    is the trusted declared schema, or the SDK base schema when omitted. Its
    digest and the error itself are checked on nonzero results. The resolver
    retrieves and decodes any stored result as a JSON object; storage and
    addressing policy belong to the host. Resolver failures use safe messages.
    """

    schema_document, schema_digest = _trusted_output_schema(output_schema)
    if result.output_schema_digest != schema_digest:
        raise _protocol_error()

    output = _resolve_container_output(result, resolve_object_reference)
    _validate_container_output(output, schema_document)
    error = _error_from_container_result(result, error_schema)
    return StandardOutputWrapper(
        result=output,
        status_code=result.status_code,
        status_message=result.status_message,
        error_result=error,
    )


class ContainerRemoteError(RuntimeError):
    """A terminal failure received from an untrusted workload container."""

    def __init__(
        self,
        code: FailureCode,
        *,
        retryable: bool,
        outcome: str,
        source: Literal["workload", "protocol", "transport"],
        message: str = "",
        provider_code: str = "",
    ) -> None:
        super().__init__(INCOMPLETE_STEP_MESSAGE)
        self.code = code
        self.retryable = retryable
        self.outcome = outcome
        self.source = source
        # The workload's safe, bounded failure message; empty when only the
        # category is known, so consumers can display the reason without
        # trusting it.
        self.message = message
        # The workload's bounded provider correlation code, empty when absent.
        self.provider_code = provider_code


ContainerStepHandler = Callable[[ContainerInvocation], ContainerResult]


class StepRegistry:
    """Maps exact type revision IDs to handlers."""

    def __init__(
        self,
        steps: Mapping[str, ContainerStepHandler],
    ) -> None:
        self._steps = dict(steps)
        if not self._steps:
            raise ValueError("at least one step type revision must be registered")
        for step_revision_id, handler in self._steps.items():
            if not _IDENTITY.fullmatch(step_revision_id):
                raise ValueError("step type revision IDs must be Syntara identities")
            if not callable(handler):
                raise TypeError("registered step type handlers must be callable")

    @property
    def step_revision_ids(self) -> tuple[str, ...]:
        """Stable exact registered identities."""

        return tuple(sorted(self._steps))

    def resolve(self, step_revision_id: str) -> ContainerStepHandler:
        handler = self._steps.get(step_revision_id)
        if handler is not None:
            return handler
        raise ContainerInvocationError(
            FailureCode.NOT_FOUND,
            "The requested step type is not available in this container image.",
            outcome="not_started",
        )


def _close_sync_invocation(
    cancellation: Event,
    provider: CallbackCredentialProvider,
    outgoing: Queue[container_pb2.ExecuteServerMessage | None],
) -> None:
    cancellation.set()
    provider.close()
    outgoing.put(None)


def _receive_sync_responses(
    requests: Iterator[container_pb2.ExecuteClientMessage],
    provider: CallbackCredentialProvider,
    invocation: ContainerInvocation,
    outgoing: Queue[container_pb2.ExecuteServerMessage | None],
    terminal_lock: LockType,
) -> None:
    try:
        for message in requests:
            if message.WhichOneof("message") != "credential_response":
                with terminal_lock:
                    invocation.cancellation.set()
                    outgoing.put(container_pb2.ExecuteServerMessage(event=failure_event(
                        invocation.invocation_id, ContainerInvocationError(
                            FailureCode.CANCELLED, _CANCELLED_STEP_MESSAGE, outcome="not_completed"
                        )
                    )))
                    outgoing.put(None)
                return
            provider.receive(message.credential_response)
    except Exception:
        pass
    finally:
        provider.close()


def _run_sync_step(
    handler: ContainerStepHandler,
    invocation: ContainerInvocation,
    provider: CallbackCredentialProvider,
    outgoing: Queue[container_pb2.ExecuteServerMessage | None],
    terminal_lock: LockType,
) -> None:
    try:
        result = handler(invocation)
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
    with terminal_lock:
        outgoing.put(container_pb2.ExecuteServerMessage(event=event))
        outgoing.put(None)


@dataclass
class _SyncWorkerGroup:
    remaining: int = 2


class ContainerRuntimeService(container_pb2_grpc.ContainerServiceServicer):
    """Bidirectional invocation service on an authenticated, confidential channel."""

    def __init__(self, steps: StepRegistry) -> None:
        self._steps = steps
        self._active: dict[str, tuple[Event, CallbackCredentialProvider, Queue[container_pb2.ExecuteServerMessage | None], LockType]] = {}
        self._active_lock = Lock()
        self._shutdown = Event()
        self._worker_threads: set[Thread] = set()
        self._worker_threads_lock = Lock()

    def Execute(
        self, requests: Iterator[container_pb2.ExecuteClientMessage], context: grpc.ServicerContext
    ) -> Iterator[container_pb2.ExecuteServerMessage]:
        invocation_id = ""
        cancellation: Event | None = None
        provider: CallbackCredentialProvider | None = None
        outgoing: Queue[container_pb2.ExecuteServerMessage | None] = Queue()
        # Keep each terminal event adjacent to its stream terminator.
        terminal_lock = Lock()
        try:
            first = next(requests)
            if first.WhichOneof("message") != "start":
                raise ContainerInvocationError(FailureCode.INVALID_INPUT, "The invocation is invalid.", outcome="not_started")
            request = first.start
            invocation_id = request.invocation_id
            invocation, cancellation = invocation_from_request(request)
            handler = self._steps.resolve(invocation.step_revision_id)
            provider = CallbackCredentialProvider(
                invocation_id,
                lambda req: outgoing.put(container_pb2.ExecuteServerMessage(credential_request=req)),
                cancellation,
                invocation.timeout_seconds,
            )
            object.__setattr__(invocation, "credential_provider", provider)
            self._register_invocation(invocation_id, cancellation, provider, outgoing, terminal_lock)

            def close() -> None:
                _close_sync_invocation(invocation.cancellation, provider, outgoing)

            if not context.add_callback(close):
                close()

            self._start_invocation_workers(
                requests, handler, invocation, provider, outgoing, terminal_lock
            )
            while context.is_active():
                message = outgoing.get()
                if message is None:
                    break
                yield message
        except StopIteration:
            return
        except ContainerInvocationError as error:
            yield container_pb2.ExecuteServerMessage(event=failure_event(invocation_id, error))
        except Exception:
            yield container_pb2.ExecuteServerMessage(event=failure_event(
                invocation_id, ContainerInvocationError(
                    FailureCode.OUTCOME_UNKNOWN, INCOMPLETE_STEP_MESSAGE, outcome="unknown"
                )
            ))
        finally:
            if cancellation is not None:
                cancellation.set()
                if provider is not None:
                    provider.close()
                self._unregister_invocation(invocation_id, cancellation)

    def Cancel(
        self, request: container_pb2.CancelRequest, context: grpc.ServicerContext
    ) -> container_pb2.CancelResponse:
        with self._active_lock:
            active = self._active.get(request.invocation_id)
        if active is None:
            return container_pb2.CancelResponse(accepted=False)
        cancellation, provider, outgoing, terminal_lock = active
        with terminal_lock:
            cancellation.set()
            provider.close()
            outgoing.put(container_pb2.ExecuteServerMessage(event=failure_event(
                request.invocation_id, ContainerInvocationError(
                    FailureCode.CANCELLED, _CANCELLED_STEP_MESSAGE, outcome="not_completed"
                )
            )))
            outgoing.put(None)
        return container_pb2.CancelResponse(accepted=True)

    def shutdown(self) -> None:
        """Revoke callbacks and wait briefly for owned sync workers to stop.

        Worker threads are daemon threads with a fixed process-wide concurrency
        cap. Python cannot stop arbitrary handler code, so a blocked handler
        retains its capacity slot until it returns.
        """
        with self._worker_threads_lock:
            self._shutdown.set()
            workers = tuple(self._worker_threads)
        with self._active_lock:
            active = list(self._active.items())
        for invocation_id, (cancellation, provider, outgoing, terminal_lock) in active:
            with terminal_lock:
                cancellation.set()
                provider.close()
                outgoing.put(container_pb2.ExecuteServerMessage(event=failure_event(
                    invocation_id, ContainerInvocationError(
                        FailureCode.CANCELLED, _CANCELLED_STEP_MESSAGE, outcome="not_completed"
                    )
                )))
                outgoing.put(None)
        deadline = monotonic() + 1.0
        for worker in workers:
            if worker is not current_thread() and worker.ident is not None:
                worker.join(timeout=max(0.0, deadline - monotonic()))

    def _start_invocation_workers(
        self,
        requests: Iterator[container_pb2.ExecuteClientMessage],
        handler: ContainerStepHandler,
        invocation: ContainerInvocation,
        provider: CallbackCredentialProvider,
        outgoing: Queue[container_pb2.ExecuteServerMessage | None],
        terminal_lock: LockType,
    ) -> None:
        group = _SyncWorkerGroup()
        try:
            reader = Thread(
                target=self._run_owned_sync_worker,
                args=(
                    group,
                    _receive_sync_responses,
                    (requests, provider, invocation, outgoing, terminal_lock),
                ),
                daemon=True,
                name="syntara-runtime-reader",
            )
            step = Thread(
                target=self._run_owned_sync_worker,
                args=(
                    group,
                    _run_sync_step,
                    (handler, invocation, provider, outgoing, terminal_lock),
                ),
                daemon=True,
                name="syntara-runtime-step",
            )
        except Exception:
            _SYNC_RUNTIME_WORKER_SLOTS.release()
            raise
        startup_error: Exception | None = None
        unstarted: tuple[Thread, ...] = ()
        try:
            with self._worker_threads_lock:
                if self._shutdown.is_set():
                    _SYNC_RUNTIME_WORKER_SLOTS.release()
                    return
                self._worker_threads.update((reader, step))
                started = 0
                try:
                    reader.start()
                    started += 1
                    step.start()
                    started += 1
                except Exception as error:
                    startup_error = error
                    unstarted = (reader, step)[started:]
                    self._worker_threads.difference_update(unstarted)
        except Exception:
            _SYNC_RUNTIME_WORKER_SLOTS.release()
            raise
        if startup_error is not None:
            for _worker in unstarted:
                self._finish_sync_worker(group)
            raise startup_error

    def _run_owned_sync_worker(
        self,
        group: _SyncWorkerGroup,
        target: Callable[..., None],
        args: tuple[Any, ...],
    ) -> None:
        try:
            target(*args)
        finally:
            with self._worker_threads_lock:
                self._worker_threads.discard(current_thread())
            self._finish_sync_worker(group)

    def _finish_sync_worker(self, group: _SyncWorkerGroup) -> None:
        with self._worker_threads_lock:
            group.remaining -= 1
            if group.remaining == 0:
                _SYNC_RUNTIME_WORKER_SLOTS.release()

    def Health(
        self, request: container_pb2.HealthRequest, context: grpc.ServicerContext
    ) -> container_pb2.HealthResponse:
        return container_pb2.HealthResponse(
            ready=True,
            runtime_abi=CONTAINER_ABI,
            step_revision_ids=self._steps.step_revision_ids,
        )

    def _register_invocation(self, invocation_id: str, cancellation: Event, provider: CallbackCredentialProvider, outgoing: Queue[container_pb2.ExecuteServerMessage | None], terminal_lock: LockType) -> None:
        with self._active_lock:
            if self._shutdown.is_set():
                raise ContainerInvocationError(
                    FailureCode.RATE_LIMITED,
                    "The runtime is shutting down.",
                    retryable=True,
                    outcome="not_started",
                )
            if invocation_id in self._active:
                raise ContainerInvocationError(
                    FailureCode.CONFLICT,
                    "The step is already running.",
                    outcome="not_started",
                )
            if not _SYNC_RUNTIME_WORKER_SLOTS.acquire(blocking=False):
                raise ContainerInvocationError(
                    FailureCode.RATE_LIMITED,
                    "The runtime is at its concurrency limit.",
                    retryable=True,
                    outcome="not_started",
                )
            self._active[invocation_id] = (cancellation, provider, outgoing, terminal_lock)

    def _unregister_invocation(self, invocation_id: str, cancellation: Event) -> None:
        with self._active_lock:
            active = self._active.get(invocation_id)
            if active is not None and active[0] is cancellation:
                del self._active[invocation_id]


def terminal_execution_event(
    invocation: ContainerInvocation,
    result: ContainerResult | None,
) -> container_pb2.ExecutionEvent:
    try:
        if invocation.cancelled:
            raise ContainerInvocationError(
                FailureCode.CANCELLED,
                    _CANCELLED_STEP_MESSAGE,
                outcome="not_completed",
            )
        if not isinstance(result, ContainerResult):
            raise ContainerInvocationError(
                FailureCode.ABI_VIOLATION,
                    "The step returned an invalid response.",
                outcome="unknown",
            )
        return _result_event(invocation.invocation_id, result)
    except ContainerInvocationError as error:
        return failure_event(invocation.invocation_id, error)
    except Exception:
        return failure_event(
            invocation.invocation_id,
            ContainerInvocationError(
                FailureCode.OUTCOME_UNKNOWN,
                INCOMPLETE_STEP_MESSAGE,
                outcome="unknown",
            ),
        )


def add_container_runtime_service(
    server: grpc.Server,
    steps: StepRegistry,
    *,
    install_sigterm_shutdown: bool = True,
    shutdown_grace_seconds: float = 5,
) -> ContainerRuntimeService:
    """Register the generic runtime service on a plugin workload gRPC server.

    Workload images built with this helper usually run their Python process
    as PID 1 in the container. The kernel ignores unhandled signals for
    PID 1, and CPython installs no SIGTERM handler, so without an explicit
    handler the container survives SIGTERM until the orchestrator's SIGKILL
    at the end of its termination grace period — stalling every dispatch
    teardown by the full grace window.

    To prevent that, this helper also installs a SIGTERM handler that stops
    ``server``, but only when it runs as PID 1: that is the only process
    whose unhandled SIGTERM the kernel discards, and installing a global
    handler in a host process would hijack its signal policy (a test
    harness call inside a pytest session must not stop the session from
    terminating on SIGTERM). Host-process callers such as testing harnesses
    pass ``install_sigterm_shutdown=False`` to make the opt-out explicit.
    A handler the caller installed itself is never overridden. The returned
    service owns its invocation workers; call ``service.shutdown()`` during
    server teardown to revoke callbacks and wait briefly for those workers.
    """

    service = ContainerRuntimeService(steps)
    container_pb2_grpc.add_ContainerServiceServicer_to_server(service, server)
    if install_sigterm_shutdown:
        _install_sigterm_shutdown(server, shutdown_grace_seconds, service)
    return service


def _install_sigterm_shutdown(server: grpc.Server, grace: float, service: ContainerRuntimeService) -> None:
    """Stop ``server`` on SIGTERM when running as the container's PID 1."""

    def _stop_server(_signum: int, _frame: object) -> None:
        stopped = server.stop(grace)

        def revoke_after_stop() -> None:
            stopped.wait()
            service.shutdown()

        Thread(target=revoke_after_stop, daemon=True).start()

    # Only PID 1 needs this: the kernel ignores unhandled signals for PID 1,
    # so without a handler SIGTERM never reaches the process. Every other
    # process already terminates on SIGTERM by default, and installing a
    # global handler there would hijack the host's signal policy.
    if os.getpid() != 1:
        return
    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
        return
    try:
        signal.signal(signal.SIGTERM, _stop_server)
    except ValueError:
        # Not the main thread (or a restricted platform): the caller keeps
        # responsibility for the server lifecycle.
        return


class _CredentialRequestBridge:
    """Keep one invocation's host responses outside the business event stream."""

    def __init__(
        self, invocation_id: str,
        credential_handler: CredentialResolver | None,
        start: container_pb2.ExecuteRequest,
        cancellation: Event,
        deadline: float,
    ) -> None:
        self._invocation_id = invocation_id
        self._credential_handler = credential_handler
        self._cancellation = cancellation
        self._deadline = deadline
        self._outgoing: Queue[container_pb2.ExecuteClientMessage | None] = Queue()
        self._outgoing.put(container_pb2.ExecuteClientMessage(start=start))
        self._active = Event()
        self._active.set()
        self._pending: set[str] = set()
        self._seen: set[str] = set()
        self._lock = Lock()

    def requests(self) -> Iterator[container_pb2.ExecuteClientMessage]:
        while True:
            message = self._outgoing.get()
            if message is None:
                return
            if message.WhichOneof("message") == "credential_response":
                # Serialize the credential handoff with close(): once close
                # returns, no response can still be crossing this boundary.
                with self._lock:
                    if not self._active.is_set():
                        return
                    yield message
            else:
                if not self._active.is_set():
                    return
                yield message

    @property
    def has_pending(self) -> bool:
        """Whether a credential resolver is still running for this invocation."""

        with self._lock:
            return bool(self._pending)

    def accept(self, request: container_pb2.CredentialRequest) -> None:
        if (request.invocation_id != self._invocation_id
                or not _IDENTITY.fullmatch(request.request_id)
                or not _IDENTITY.fullmatch(request.name)):
            raise _protocol_error()
        with self._lock:
            if (request.request_id in self._seen or len(self._seen) >= 4096
                    or len(self._pending) >= 16 or not self._active.is_set()):
                raise _protocol_error()
            self._pending.add(request.request_id)
            self._seen.add(request.request_id)
        self._respond(request)

    def _respond(self, request: container_pb2.CredentialRequest) -> None:
        response = container_pb2.CredentialResponse(
            invocation_id=self._invocation_id, request_id=request.request_id
        )
        context = CredentialRequestContext(
            invocation_id=self._invocation_id,
            name=request.name,
            cancellation=self._cancellation,
            deadline=self._deadline,
        )
        with self._lock:
            credential_handler = (
                self._credential_handler if self._active.is_set() else None
            )
        try:
            if credential_handler is None or context.cancelled:
                raise CredentialAccessError("missing")
            value = credential_handler(context)
            response.document_json = _encode_json_object(value, "credential")
        except CredentialAccessError as error:
            response.error = (container_pb2.CREDENTIAL_ERROR_MISSING
                if error.reason == "missing" else container_pb2.CREDENTIAL_ERROR_UNAVAILABLE)
        except Exception:
            response.error = container_pb2.CREDENTIAL_ERROR_UNAVAILABLE
        self._complete_response(request, response, context)

    def _complete_response(
        self,
        request: container_pb2.CredentialRequest,
        response: container_pb2.CredentialResponse,
        context: CredentialRequestContext,
    ) -> None:
        with self._lock:
            self._pending.discard(request.request_id)
            if self._active.is_set() and not context.cancelled:
                self._outgoing.put(container_pb2.ExecuteClientMessage(credential_response=response))

    def close(self, *, cancelled: bool = False) -> None:
        with self._lock:
            self._active.clear()
            self._pending.clear()
            self._credential_handler = None
            self._outgoing.put(None)
            if cancelled:
                self._cancellation.set()


def _execution_event_from_stream(
    event: container_pb2.ExecutionEvent, invocation_id: str,
    saw_terminal: bool,
) -> ContainerProgress | ContainerResult:
    if event.invocation_id != invocation_id or saw_terminal:
        raise _protocol_error()
    kind = event.WhichOneof("event")
    if kind == "progress":
        return _progress_from_event(event)
    if kind == "result":
        return _result_from_event(event)
    if kind == "failure":
        raise _remote_failure(event)
    raise _protocol_error()


class ContainerClient:
    """Synchronous client for an Execution Plane adapter or local test harness.

    Callers supply an already configured gRPC channel.  That keeps transport
    policy, including mTLS and workload-network rules, with the Execution Plane
    rather than in untrusted plugin code.

    Each invocation can supply a credential handler that resolves a declared
    recipe ID within that invocation's authority and returns a JSON object.
    The handler receives a :class:`CredentialRequestContext` with the request
    name, cancellation signal, and invocation deadline. It should use the
    remaining time to bound its operation and network calls. The client invokes
    the handler on the caller's stream-consumption thread; caller-owned runners
    should apply their own executor and queue limits.
    """

    def __init__(self, channel: grpc.Channel) -> None:
        self._channel = channel
        self._stub = container_pb2_grpc.ContainerServiceStub(channel)
        self._active_bridges: dict[str, set[_CredentialRequestBridge]] = {}
        self._active_bridges_lock = Lock()

    def _ensure_bridge_state(self) -> None:
        """Initialize bridge tracking for lightweight test doubles too."""

        if not hasattr(self, "_active_bridges_lock"):
            self._active_bridges = {}
            self._active_bridges_lock = Lock()

    def _register_bridge(self, bridge: _CredentialRequestBridge) -> None:
        self._ensure_bridge_state()
        with self._active_bridges_lock:
            self._active_bridges.setdefault(bridge._invocation_id, set()).add(bridge)

    def _unregister_bridge(self, bridge: _CredentialRequestBridge) -> None:
        self._ensure_bridge_state()
        with self._active_bridges_lock:
            bridges = self._active_bridges.get(bridge._invocation_id)
            if bridges is None:
                return
            bridges.discard(bridge)
            if not bridges:
                self._active_bridges.pop(bridge._invocation_id, None)

    def _close_local_bridges(self, invocation_id: str) -> None:
        self._ensure_bridge_state()
        with self._active_bridges_lock:
            bridges = tuple(self._active_bridges.get(invocation_id, ()))
        for bridge in bridges:
            bridge.close(cancelled=True)

    def health(self, *, timeout_seconds: float | None = None) -> tuple[str, ...]:
        """Check the ABI identifier and return the advertised step type identities.

        Matching identifiers do not distinguish the earlier unary Execute
        protocol from the current bidirectional v1alpha1 protocol.
        """

        rpc_failure: ContainerRemoteError | None = None
        try:
            response = self._stub.Health(container_pb2.HealthRequest(), timeout=timeout_seconds)
        except grpc.RpcError as error:
            rpc_failure = _rpc_error(error)
        if rpc_failure is not None:
            raise rpc_failure
        if not response.ready or response.runtime_abi != CONTAINER_ABI:
            raise ContainerRemoteError(
                FailureCode.ABI_VIOLATION,
                retryable=False,
                outcome="not_started",
                source="protocol",
            )
        return tuple(response.step_revision_ids)

    def execute(
        self, invocation: ContainerInvocation, *, timeout_seconds: float | None = None,
        credential_handler: CredentialResolver | None = None,
    ) -> ContainerResult:
        """Invoke a step type and return its sole terminal result."""

        for event in self.stream(
            invocation, timeout_seconds=timeout_seconds, credential_handler=credential_handler
        ):
            if isinstance(event, ContainerResult):
                return event
        raise ContainerRemoteError(
            FailureCode.OUTCOME_UNKNOWN,
            retryable=False,
            outcome="unknown",
            source="protocol",
        )

    def execute_wrapped(
        self,
        invocation: ContainerInvocation,
        *,
        output_schema: Mapping[str, Any],
        error_schema: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
        resolve_object_reference: Callable[[str], Mapping[str, Any]] | None = None,
        credential_handler: CredentialResolver | None = None,
    ) -> StandardOutputWrapper:
        """Invoke a step type and reconstruct its SDK-owned output wrapper.

        ``output_schema`` and an optional ``error_schema`` must be trusted
        schemas associated with this invocation. Their canonical digests are
        checked before validating the corresponding result.

        Remote and transport failures still raise ``ContainerRemoteError``.
        Referenced output requires a host-supplied resolver.
        """

        result = self.execute(
            invocation, timeout_seconds=timeout_seconds, credential_handler=credential_handler
        )
        return standard_output_from_container_result(
            result,
            output_schema=output_schema,
            error_schema=error_schema,
            resolve_object_reference=resolve_object_reference,
        )

    def stream(
        self, invocation: ContainerInvocation, *, timeout_seconds: float | None = None,
        credential_handler: CredentialResolver | None = None,
    ) -> Iterator[ContainerProgress | ContainerResult]:
        """Invoke a step type and yield progress followed by exactly one result."""

        request = _request_from_invocation(invocation)
        deadline = _execution_deadline(invocation, timeout_seconds)
        bridge = _CredentialRequestBridge(
            invocation.invocation_id,
            credential_handler,
            request,
            invocation.cancellation,
            monotonic() + deadline,
        )
        self._register_bridge(bridge)
        saw_terminal = False
        rpc_failure: ContainerRemoteError | None = None
        call = None
        try:
            call = self._stub.Execute(bridge.requests(), timeout=deadline)
            add_done_callback = getattr(call, "add_callback", None)
            if callable(add_done_callback):
                add_done_callback(lambda: bridge.close(cancelled=bridge.has_pending))
            for message in call:
                event, saw_terminal = self._stream_event(
                    message, bridge, invocation.invocation_id, saw_terminal
                )
                if event is not None:
                    yield event
        except grpc.RpcError as error:
            rpc_failure = _rpc_error(error)
        finally:
            bridge.close(cancelled=not saw_terminal or rpc_failure is not None)
            self._unregister_bridge(bridge)
            if call is not None:
                call.cancel()
        if rpc_failure is not None:
            raise rpc_failure
        if not saw_terminal:
            raise _protocol_error()

    @staticmethod
    def _stream_event(
        message: container_pb2.ExecuteServerMessage,
        bridge: _CredentialRequestBridge,
        invocation_id: str,
        saw_terminal: bool,
    ) -> tuple[ContainerProgress | ContainerResult | None, bool]:
        """Validate one server message and return its public stream event."""

        kind = message.WhichOneof("message")
        if kind == "credential_request":
            if saw_terminal:
                raise _protocol_error()
            bridge.accept(message.credential_request)
            return None, saw_terminal
        if kind != "event":
            raise _protocol_error()
        event = _execution_event_from_stream(message.event, invocation_id, saw_terminal)
        return event, saw_terminal or isinstance(event, ContainerResult)

    def cancel(self, invocation_id: str, *, timeout_seconds: float | None = None) -> bool:
        """Request cooperative cancellation for an active step."""

        if not _IDENTITY.fullmatch(invocation_id):
            raise ValueError("invocation_id must be a Syntara identity")
        rpc_failure: ContainerRemoteError | None = None
        try:
            response = self._stub.Cancel(
                container_pb2.CancelRequest(invocation_id=invocation_id),
                timeout=timeout_seconds,
            )
        except grpc.RpcError as error:
            rpc_failure = _rpc_error(error)
        if rpc_failure is not None:
            raise rpc_failure
        if response.accepted:
            self._close_local_bridges(invocation_id)
        return response.accepted


def invocation_from_request(
    request: container_pb2.ExecuteRequest,
) -> tuple[ContainerInvocation, Event]:
    try:
        _require_identity(request.invocation_id, "invocation_id")
        _require_identity(request.workflow_run_id, "workflow_run_id")
        _require_identity(request.step_revision_id, "step_revision_id")
    except ValueError as error:
        raise ContainerInvocationError(
            FailureCode.INVALID_INPUT,
            "The step identity is invalid.",
            outcome="not_started",
        ) from error
    if request.runtime_abi != CONTAINER_ABI:
        raise ContainerInvocationError(
            FailureCode.ABI_VIOLATION,
            "The plugin container does not support the requested runtime ABI.",
            outcome="not_started",
        )
    if not 1 <= request.timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise ContainerInvocationError(
            FailureCode.INVALID_INPUT,
            "The step timeout is outside the supported range.",
            outcome="not_started",
        )
    inputs = _decode_json_object(request.inputs_json, "inputs")
    runtime_context = _decode_json_object(request.runtime_context_json, "runtime context")
    cancellation = Event()
    invocation = ContainerInvocation(
        invocation_id=request.invocation_id,
        workflow_run_id=request.workflow_run_id,
        step_revision_id=request.step_revision_id,
        inputs=inputs,
        timeout_seconds=request.timeout_seconds,
        runtime_context=runtime_context,
        cancellation=cancellation,
    )
    return invocation, cancellation


def _request_from_invocation(
    invocation: ContainerInvocation,
) -> container_pb2.ExecuteRequest:
    _require_identity(invocation.invocation_id, "invocation_id")
    _require_identity(invocation.workflow_run_id, "workflow_run_id")
    _require_identity(invocation.step_revision_id, "step_revision_id")
    if not 1 <= invocation.timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout_seconds is outside the supported range")
    return container_pb2.ExecuteRequest(
        invocation_id=invocation.invocation_id,
        workflow_run_id=invocation.workflow_run_id,
        step_revision_id=invocation.step_revision_id,
        runtime_abi=CONTAINER_ABI,
        inputs_json=_encode_json_object(invocation.inputs, "inputs"),
        timeout_seconds=invocation.timeout_seconds,
        runtime_context_json=_encode_json_object(invocation.runtime_context, "runtime context"),
    )


def _execution_deadline(invocation: ContainerInvocation, timeout_seconds: float | None) -> float:
    """Constrain transport lifetime to the invocation contract's timeout."""

    deadline = float(invocation.timeout_seconds)
    if timeout_seconds is None:
        return deadline
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
        raise ValueError("timeout_seconds must be a positive number")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive number")
    return min(deadline, float(timeout_seconds))


def _decode_json_object(value: bytes, field_name: str) -> dict[str, Any]:
    if len(value) > _MAX_JSON_BYTES:
        raise ContainerInvocationError(
            FailureCode.INVALID_INPUT,
            f"The {field_name} payload is too large.",
            outcome="not_started",
        )
    invalid_json = False
    decoded: Any = None
    try:
        decoded = json.loads(
            value or b"{}",
            parse_constant=_reject_nonstandard_json_constant,
            object_pairs_hook=_reject_duplicate_json_members,
        )
        canonical_json_bytes(decoded)
    except (ValueError, RecursionError):
        invalid_json = True
    if invalid_json:
        raise ContainerInvocationError(
            FailureCode.INVALID_INPUT,
            f"The {field_name} payload must be a JSON object.",
            outcome="not_started",
        )
    if not isinstance(decoded, dict):
        raise ContainerInvocationError(
            FailureCode.INVALID_INPUT,
            f"The {field_name} payload must be a JSON object.",
            outcome="not_started",
        )
    return decoded


def _reject_nonstandard_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON number {value!r}")


def _reject_duplicate_json_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous objects instead of accepting the last repeated key."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object keys must be unique")
        result[key] = value
    return result


def _encode_json_object(value: Mapping[str, Any], field_name: str) -> bytes:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    try:
        validate_json_object(value)
        encoded = canonical_json_bytes(_thaw_json_document(value))
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must contain JSON-compatible values") from None
    if len(encoded) > _MAX_JSON_BYTES:
        raise ValueError(f"{field_name} payload is too large")
    return encoded


def _require_identity(value: str, field_name: str) -> None:
    if not _IDENTITY.fullmatch(value):
        raise ValueError(f"{field_name} must be a Syntara identity")


def _result_event(invocation_id: str, result: ContainerResult) -> container_pb2.ExecutionEvent:
    invalid_output = False
    output_json = b""
    error_result_json = b""
    try:
        output_json = (
            _encode_json_object(result.output, "output") if result.output is not None else b""
        )
        error_result_json = (
            _encode_json_object(result.error_result, "error result")
            if result.error_result is not None else b""
        )
    except ValueError:
        invalid_output = True
    if invalid_output:
        raise ContainerInvocationError(
            FailureCode.ABI_VIOLATION,
            "The step returned an invalid response.",
            outcome="unknown",
        )
    return container_pb2.ExecutionEvent(
        invocation_id=invocation_id,
        result=container_pb2.Result(
            output_schema_digest=result.output_schema_digest,
            output_json=output_json,
            object_reference=result.object_reference or "",
            status_code=result.status_code,
            status_message=result.status_message,
            error_result_json=error_result_json,
            error_schema_digest=result.error_schema_digest,
        ),
    )


def failure_event(
    invocation_id: str, error: ContainerInvocationError
) -> container_pb2.ExecutionEvent:
    return container_pb2.ExecutionEvent(
        invocation_id=invocation_id,
        failure=container_pb2.Failure(
            code=error.code.value,
            message=error.safe_message,
            retryable=error.retryable,
            outcome=error.outcome,
            correlation_id=invocation_id,
            provider_code=error.provider_code,
        ),
    )


def _progress_from_event(event: container_pb2.ExecutionEvent) -> ContainerProgress:
    progress = event.progress
    if (
        event.invocation_id == ""
        or progress.sequence < 1
        or progress.state not in _PROGRESS_STATES
        or len(progress.message) > MAX_MESSAGE_LENGTH
    ):
        raise _protocol_error()
    if progress.has_percent and not 0 <= progress.percent <= 100:
        raise _protocol_error()
    return ContainerProgress(
        invocation_id=event.invocation_id,
        sequence=progress.sequence,
        state=progress.state,
        message=progress.message,
        percent=progress.percent if progress.has_percent else None,
    )


def _result_from_event(event: container_pb2.ExecutionEvent) -> ContainerResult:
    result = event.result
    has_output = bool(result.output_json)
    has_reference = bool(result.object_reference)
    if has_output and has_reference:
        raise _protocol_error()
    invalid_result = False
    decoded_result: ContainerResult | None = None
    try:
        output = _decode_json_object(result.output_json, "output") if has_output else None
        decoded_result = ContainerResult(
            output_schema_digest=result.output_schema_digest,
            output=output,
            object_reference=result.object_reference or None,
            status_code=result.status_code,
            status_message=result.status_message,
            error_result=_decode_json_object(result.error_result_json, "error result")
            if result.error_result_json else None,
            error_schema_digest=result.error_schema_digest,
        )
    except (ContainerInvocationError, ValueError, TypeError):
        invalid_result = True
    if invalid_result or decoded_result is None:
        raise _protocol_error()
    return decoded_result


def _remote_failure(event: container_pb2.ExecutionEvent) -> ContainerRemoteError:
    invalid_failure_code = False
    code: FailureCode | None = None
    try:
        code = FailureCode(event.failure.code)
    except ValueError:
        invalid_failure_code = True
    if invalid_failure_code or code is None:
        raise _protocol_error()
    if event.failure.outcome not in {"not_started", "not_completed", "unknown"}:
        raise _protocol_error()
    if not event.failure.message or len(event.failure.message) > MAX_MESSAGE_LENGTH:
        # The ABI bounds the failure message like every other remote string;
        # an over-long message is a non-conforming peer, not a valid failure.
        raise _protocol_error()
    if event.failure.provider_code and not _PROVIDER_CODE.fullmatch(
        event.failure.provider_code
    ):
        raise _protocol_error()
    return ContainerRemoteError(
        code,
        retryable=event.failure.retryable,
        outcome=event.failure.outcome,
        source="workload",
        message=event.failure.message,
        provider_code=event.failure.provider_code,
    )


def _protocol_error() -> ContainerRemoteError:
    return ContainerRemoteError(
        FailureCode.ABI_VIOLATION,
        retryable=False,
        outcome="unknown",
        source="protocol",
    )


def _rpc_error(error: grpc.RpcError) -> ContainerRemoteError:
    if error.code() is grpc.StatusCode.DEADLINE_EXCEEDED:
        return ContainerRemoteError(
            FailureCode.TIMEOUT,
            retryable=True,
            outcome="unknown",
            source="transport",
        )
    if error.code() is grpc.StatusCode.CANCELLED:
        return ContainerRemoteError(
            FailureCode.CANCELLED,
            retryable=False,
            outcome="unknown",
            source="transport",
        )
    return ContainerRemoteError(
        FailureCode.UPSTREAM_TRANSIENT,
        retryable=True,
        outcome="unknown",
        source="transport",
    )
