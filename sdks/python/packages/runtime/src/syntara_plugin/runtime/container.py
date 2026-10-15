"""The generic container runtime API for Syntara plugin workloads.

One container image may register many immutable step revisions.  The Execution
Plane selects the revision in an :class:`ContainerInvocation`; the runtime
dispatches to the matching Python callable. The protocol deliberately carries
only JSON input data, bounded non-secret runtime context, and paths to
credential files mounted by the Execution Plane. It never carries commands,
environment-derived credentials, or plaintext secret material.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import Any, Literal, NoReturn

import grpc

from .abi import CONTAINER_ABI, FailureCode
from .protocol import container_pb2, container_pb2_grpc

_IDENTITY = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_OUTPUT_SCHEMA_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_CREDENTIAL_PATH = re.compile(r"^/var/run/syntara/secrets/[A-Za-z0-9._-]+$")
_PROVIDER_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_MAX_JSON_BYTES = 1_048_576
_MAX_TIMEOUT_SECONDS = 86_400
_PROGRESS_STATES = frozenset({"started", "heartbeat", "progress"})

# A platform-owned runner can handle any installed step that the control
# plane has already resolved to that runner. It is a health-advertisement
# capability only, never a valid ``step_revision_id`` in an Execute request.
ANY_STEP_REVISION = "*"


@dataclass(frozen=True)
class ContainerInvocation:
    """The selected plugin step and the safe data supplied to it."""

    invocation_id: str
    workflow_run_id: str
    step_revision_id: str
    inputs: Mapping[str, Any]
    credential_file_paths: Mapping[str, str]
    timeout_seconds: int
    runtime_context: Mapping[str, Any] = field(default_factory=dict)
    cancellation: Event = field(default_factory=Event, compare=False, repr=False)

    @property
    def cancelled(self) -> bool:
        """Whether the Execution Plane has requested cooperative cancellation."""

        return self.cancellation.is_set()


@dataclass(frozen=True)
class ContainerProgress:
    """A non-terminal, user-safe update emitted by a plugin step."""

    invocation_id: str
    sequence: int
    state: str
    message: str = ""
    percent: float | None = None


@dataclass(frozen=True)
class ContainerResult:
    """The sole successful terminal result of a plugin step.

    A step returns either an inline JSON object or an object-store reference,
    never both.  The output schema digest lets the control plane interpret the
    result without importing plugin Python code.
    """

    output_schema_digest: str
    output: Mapping[str, Any] | None = None
    object_reference: str | None = None

    def __post_init__(self) -> None:
        if not _OUTPUT_SCHEMA_DIGEST.fullmatch(self.output_schema_digest):
            raise ValueError("output_schema_digest must be a sha256 digest")
        if (self.output is None) == (self.object_reference is None):
            raise ValueError("exactly one of output or object_reference is required")
        if self.output is not None and not isinstance(self.output, Mapping):
            raise ValueError("output must be a JSON object")
        if self.object_reference is not None and not _IDENTITY.fullmatch(self.object_reference):
            raise ValueError("object_reference must be a Syntara identity")


class ContainerInvocationError(Exception):
    """A user-safe terminal failure a step may deliberately return."""

    def __init__(
        self,
        code: FailureCode,
        message: str,
        *,
        retryable: bool = False,
        outcome: str = "not_completed",
        provider_code: str = "",
    ) -> None:
        super().__init__(message)
        if not isinstance(code, FailureCode):
            raise TypeError("code must be a FailureCode")
        if not message or len(message) > 4_096:
            raise ValueError("message must contain at most 4096 characters")
        if not isinstance(retryable, bool):
            raise TypeError("retryable must be a boolean")
        if outcome not in {"not_started", "not_completed", "unknown"}:
            raise ValueError("outcome must be not_started, not_completed, or unknown")
        if provider_code and not _PROVIDER_CODE.fullmatch(provider_code):
            raise ValueError("provider_code must be a lowercase provider identifier")
        self.code = code
        self.safe_message = message
        self.retryable = retryable
        self.outcome = outcome
        self.provider_code = provider_code


class ContainerRemoteError(RuntimeError):
    """A terminal failure received from an untrusted workload container."""

    def __init__(
        self,
        code: FailureCode,
        *,
        retryable: bool,
        outcome: str,
        source: Literal["workload", "protocol", "transport"],
    ) -> None:
        super().__init__("The plugin step did not complete.")
        self.code = code
        self.retryable = retryable
        self.outcome = outcome
        self.source = source


ContainerStepHandler = Callable[[ContainerInvocation], ContainerResult]


class StepRegistry:
    """Maps immutable step revisions to handlers, with an optional fallback.

    Most publisher workload images register their exact supported revisions.
    A Syntara-managed runner may instead supply ``fallback_step``: the
    control plane has already selected the runner image and provides a
    validated runtime context for the particular installed step.  Health
    advertises :data:`ANY_STEP_REVISION` in that case so the Execution Plane
    can distinguish a generic runner from an image that omitted a revision.
    """

    def __init__(
        self,
        steps: Mapping[str, ContainerStepHandler],
        *,
        fallback_step: ContainerStepHandler | None = None,
    ) -> None:
        self._steps = dict(steps)
        self._fallback_step = fallback_step
        if not self._steps and self._fallback_step is None:
            raise ValueError("at least one step revision or a fallback step must be registered")
        for step_revision_id, handler in self._steps.items():
            if not _IDENTITY.fullmatch(step_revision_id):
                raise ValueError("step revision IDs must be Syntara identities")
            if not callable(handler):
                raise TypeError("registered step handlers must be callable")
        if self._fallback_step is not None and not callable(self._fallback_step):
            raise TypeError("fallback step handler must be callable")

    @property
    def step_revision_ids(self) -> tuple[str, ...]:
        """Stable exact identities plus generic-runner capability when present."""

        identities = tuple(sorted(self._steps))
        return identities + ((ANY_STEP_REVISION,) if self._fallback_step is not None else ())

    def resolve(self, step_revision_id: str) -> ContainerStepHandler:
        handler = self._steps.get(step_revision_id)
        if handler is not None:
            return handler
        if self._fallback_step is not None:
            return self._fallback_step
        raise ContainerInvocationError(
            FailureCode.NOT_FOUND,
            "The requested plugin step is not available in this container image.",
            outcome="not_started",
        )


class ContainerRuntimeService(container_pb2_grpc.ContainerServiceServicer):
    """gRPC implementation of the language-neutral container ABI."""

    def __init__(self, steps: StepRegistry) -> None:
        self._steps = steps
        self._active: dict[str, Event] = {}
        self._active_lock = Lock()

    def Execute(
        self, request: container_pb2.ExecuteRequest, context: grpc.ServicerContext
    ) -> Iterator[container_pb2.ExecutionEvent]:
        cancellation: Event | None = None
        invocation_id = request.invocation_id
        try:
            invocation, cancellation = _invocation_from_request(request)
            handler = self._steps.resolve(invocation.step_revision_id)
            self._register_invocation(invocation.invocation_id, cancellation)
            # Deadline expiry and a disconnected Execution Plane both become a
            # cooperative cancellation signal visible to the step.
            if not context.add_callback(cancellation.set):
                cancellation.set()
            result = handler(invocation)
            if invocation.cancelled:
                raise ContainerInvocationError(
                    FailureCode.CANCELLED,
                    "The plugin step was cancelled.",
                    outcome="not_completed",
                )
            if not isinstance(result, ContainerResult):
                raise ContainerInvocationError(
                    FailureCode.ABI_VIOLATION,
                    "The plugin step returned an invalid response.",
                    outcome="unknown",
                )
            yield _result_event(invocation.invocation_id, result)
        except ContainerInvocationError as error:
            yield _failure_event(invocation_id, error)
        except Exception:
            yield _failure_event(
                invocation_id,
                ContainerInvocationError(
                    FailureCode.OUTCOME_UNKNOWN,
                    "The plugin step did not complete.",
                    outcome="unknown",
                ),
            )
        finally:
            if cancellation is not None:
                self._unregister_invocation(invocation_id, cancellation)

    def Cancel(
        self, request: container_pb2.CancelRequest, context: grpc.ServicerContext
    ) -> container_pb2.CancelResponse:
        with self._active_lock:
            cancellation = self._active.get(request.invocation_id)
        if cancellation is None:
            return container_pb2.CancelResponse(accepted=False)
        cancellation.set()
        return container_pb2.CancelResponse(accepted=True)

    def Health(
        self, request: container_pb2.HealthRequest, context: grpc.ServicerContext
    ) -> container_pb2.HealthResponse:
        return container_pb2.HealthResponse(
            ready=True,
            runtime_abi=CONTAINER_ABI,
            step_revision_ids=self._steps.step_revision_ids,
        )

    def _register_invocation(self, invocation_id: str, cancellation: Event) -> None:
        with self._active_lock:
            if invocation_id in self._active:
                raise ContainerInvocationError(
                    FailureCode.CONFLICT,
                    "The plugin step is already running.",
                    outcome="not_started",
                )
            self._active[invocation_id] = cancellation

    def _unregister_invocation(self, invocation_id: str, cancellation: Event) -> None:
        with self._active_lock:
            if self._active.get(invocation_id) is cancellation:
                del self._active[invocation_id]


def add_container_runtime_service(
    server: grpc.Server, steps: StepRegistry
) -> ContainerRuntimeService:
    """Register the generic runtime service on a plugin workload gRPC server."""

    service = ContainerRuntimeService(steps)
    container_pb2_grpc.add_ContainerServiceServicer_to_server(service, server)
    return service


class ContainerClient:
    """Synchronous client for an Execution Plane adapter or local test harness.

    Callers supply an already configured gRPC channel.  That keeps transport
    policy, including mTLS and workload-network rules, with the Execution Plane
    rather than in untrusted plugin code.
    """

    def __init__(self, channel: grpc.Channel) -> None:
        self._channel = channel
        self._stub = container_pb2_grpc.ContainerServiceStub(channel)

    def health(self, *, timeout_seconds: float | None = None) -> tuple[str, ...]:
        """Confirm the container ABI and return the advertised step identities."""

        try:
            response = self._stub.Health(container_pb2.HealthRequest(), timeout=timeout_seconds)
        except grpc.RpcError as error:
            raise _rpc_error(error) from error
        if not response.ready or response.runtime_abi != CONTAINER_ABI:
            raise ContainerRemoteError(
                FailureCode.ABI_VIOLATION,
                retryable=False,
                outcome="not_started",
                source="protocol",
            )
        return tuple(response.step_revision_ids)

    def execute(
        self, invocation: ContainerInvocation, *, timeout_seconds: float | None = None
    ) -> ContainerResult:
        """Invoke a step and return its sole terminal result."""

        for event in self.stream(invocation, timeout_seconds=timeout_seconds):
            if isinstance(event, ContainerResult):
                return event
        raise ContainerRemoteError(
            FailureCode.OUTCOME_UNKNOWN,
            retryable=False,
            outcome="unknown",
            source="protocol",
        )

    def stream(
        self, invocation: ContainerInvocation, *, timeout_seconds: float | None = None
    ) -> Iterator[ContainerProgress | ContainerResult]:
        """Invoke a step and yield progress followed by exactly one result."""

        request = _request_from_invocation(invocation)
        deadline = _execution_deadline(invocation, timeout_seconds)
        saw_terminal = False
        try:
            for event in self._stub.Execute(request, timeout=deadline):
                if event.invocation_id != invocation.invocation_id:
                    raise _protocol_error()
                event_kind = event.WhichOneof("event")
                if event_kind == "progress":
                    yield _progress_from_event(event)
                    continue
                if event_kind == "result":
                    if saw_terminal:
                        raise _protocol_error()
                    saw_terminal = True
                    yield _result_from_event(event)
                    continue
                if event_kind == "failure":
                    raise _remote_failure(event)
                raise _protocol_error()
        except grpc.RpcError as error:
            raise _rpc_error(error) from error
        if not saw_terminal:
            raise _protocol_error()

    def cancel(self, invocation_id: str, *, timeout_seconds: float | None = None) -> bool:
        """Request cooperative cancellation for an active step."""

        if not _IDENTITY.fullmatch(invocation_id):
            raise ValueError("invocation_id must be a Syntara identity")
        try:
            response = self._stub.Cancel(
                container_pb2.CancelRequest(invocation_id=invocation_id),
                timeout=timeout_seconds,
            )
        except grpc.RpcError as error:
            raise _rpc_error(error) from error
        return response.accepted


def _invocation_from_request(
    request: container_pb2.ExecuteRequest,
) -> tuple[ContainerInvocation, Event]:
    try:
        _require_identity(request.invocation_id, "invocation_id")
        _require_identity(request.workflow_run_id, "workflow_run_id")
        _require_identity(request.step_revision_id, "step_revision_id")
    except ValueError as error:
        raise ContainerInvocationError(
            FailureCode.INVALID_INPUT,
            "The plugin step identity is invalid.",
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
            "The plugin step timeout is outside the supported range.",
            outcome="not_started",
        )
    inputs = _decode_json_object(request.inputs_json, "inputs")
    runtime_context = _decode_json_object(request.runtime_context_json, "runtime context")
    credential_file_paths = _decode_credential_file_paths(request.credential_file_paths_json)
    cancellation = Event()
    return (
        ContainerInvocation(
            invocation_id=request.invocation_id,
            workflow_run_id=request.workflow_run_id,
            step_revision_id=request.step_revision_id,
            inputs=inputs,
            credential_file_paths=credential_file_paths,
            timeout_seconds=request.timeout_seconds,
            runtime_context=runtime_context,
            cancellation=cancellation,
        ),
        cancellation,
    )


def _request_from_invocation(invocation: ContainerInvocation) -> container_pb2.ExecuteRequest:
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
        credential_file_paths_json=_encode_credential_file_paths(invocation.credential_file_paths),
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
    try:
        decoded = json.loads(
            value or b"{}",
            parse_constant=_reject_nonstandard_json_constant,
        )
    except ValueError as error:
        raise ContainerInvocationError(
            FailureCode.INVALID_INPUT,
            f"The {field_name} payload must be a JSON object.",
            outcome="not_started",
        ) from error
    if not isinstance(decoded, dict):
        raise ContainerInvocationError(
            FailureCode.INVALID_INPUT,
            f"The {field_name} payload must be a JSON object.",
            outcome="not_started",
        )
    return decoded


def _reject_nonstandard_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON number {value!r}")


def _decode_credential_file_paths(value: bytes) -> dict[str, str]:
    paths = _decode_json_object(value, "credential file paths")
    for name, path in paths.items():
        if not isinstance(name, str) or not _IDENTITY.fullmatch(name):
            raise ContainerInvocationError(
                FailureCode.INVALID_INPUT,
                "Credential file names must be Syntara identities.",
                outcome="not_started",
            )
        if not isinstance(path, str) or not _CREDENTIAL_PATH.fullmatch(path):
            raise ContainerInvocationError(
                FailureCode.INVALID_INPUT,
                "Credential file paths must be approved secret mount paths.",
                outcome="not_started",
            )
    return paths


def _encode_json_object(value: Mapping[str, Any], field_name: str) -> bytes:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must contain JSON-compatible values") from error
    if len(encoded) > _MAX_JSON_BYTES:
        raise ValueError(f"{field_name} payload is too large")
    return encoded


def _encode_credential_file_paths(value: Mapping[str, str]) -> bytes:
    for name, path in value.items():
        if not isinstance(name, str) or not _IDENTITY.fullmatch(name):
            raise ValueError("credential file names must be Syntara identities")
        if not isinstance(path, str) or not _CREDENTIAL_PATH.fullmatch(path):
            raise ValueError("credential file paths must be approved secret mount paths")
    return _encode_json_object(value, "credential file paths")


def _require_identity(value: str, field_name: str) -> None:
    if not _IDENTITY.fullmatch(value):
        raise ValueError(f"{field_name} must be a Syntara identity")


def _result_event(invocation_id: str, result: ContainerResult) -> container_pb2.ExecutionEvent:
    try:
        output_json = (
            _encode_json_object(result.output, "output") if result.output is not None else b""
        )
    except ValueError as error:
        raise ContainerInvocationError(
            FailureCode.ABI_VIOLATION,
            "The plugin step returned an invalid response.",
            outcome="unknown",
        ) from error
    return container_pb2.ExecutionEvent(
        invocation_id=invocation_id,
        result=container_pb2.Result(
            output_schema_digest=result.output_schema_digest,
            output_json=output_json,
            object_reference=result.object_reference or "",
        ),
    )


def _failure_event(
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
        or len(progress.message) > 4_096
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
    if has_output == has_reference:
        raise _protocol_error()
    try:
        output = _decode_json_object(result.output_json, "output") if has_output else None
        return ContainerResult(
            output_schema_digest=result.output_schema_digest,
            output=output,
            object_reference=result.object_reference or None,
        )
    except (ContainerInvocationError, ValueError) as error:
        raise _protocol_error() from error


def _remote_failure(event: container_pb2.ExecutionEvent) -> ContainerRemoteError:
    try:
        code = FailureCode(event.failure.code)
    except ValueError as error:
        raise _protocol_error() from error
    if event.failure.outcome not in {"not_started", "not_completed", "unknown"}:
        raise _protocol_error()
    return ContainerRemoteError(
        code,
        retryable=event.failure.retryable,
        outcome=event.failure.outcome,
        source="workload",
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
