"""Direct and container-adapter invocation of bound steps for local tests."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import MagicMock, patch

import grpc
from syntara_plugin.runtime import (
    BaseStep,
    ErrorResult,
    ExecutionContext,
    FailureCode,
    StandardOutputWrapper,
    StepBinding,
    StepBindingError,
    invoke,
)
from syntara_plugin.runtime.adapter import container_step_for_binding
from syntara_plugin.runtime.bindings import validate_executable_step_binding
from syntara_plugin.runtime.container import (
    ContainerClient,
    ContainerInvocation,
    ContainerInvocationError,
    ContainerRemoteError,
    ContainerResult,
    ContainerStepHandler,
    StepRegistry,
    add_container_runtime_service,
)

from .bindings_factory import installed_binding
from .context_builder import build_execution_context, normalize_run_id
from .errors import HarnessUsageError
from .loader import StepTarget, resolve_step

if TYPE_CHECKING:
    from collections.abc import Iterator

Transport = Literal["direct", "container"]

# The container ABI's transport limits, enforced up front on both paths so a
# request that cannot be encoded never diverges between them.
_MAX_INVOCATION_JSON_BYTES = 1_048_576
_MIN_TIMEOUT_SECONDS = 1
_MAX_TIMEOUT_SECONDS = 86_400


def _json_round_trip(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    """Normalize one mapping through JSON and check the transport limits.

    The round trip makes both execution paths deliver identical JSON-native
    values: tuples arrive as lists and non-string mapping keys as strings,
    exactly as the container ABI decodes them, instead of the direct path
    observing the original Python objects. It also hands each path its own
    copy, so a step mutating nested runtime-context data on one path cannot
    leak into the other.
    """

    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        message = (
            f"{field} must contain only JSON-compatible values so both execution "
            "paths accept it"
        )
        raise HarnessUsageError(message) from error
    if len(encoded.encode("utf-8")) > _MAX_INVOCATION_JSON_BYTES:
        message = (
            f"{field} is too large for the container ABI ({len(encoded)} encoded "
            f"bytes, limit {_MAX_INVOCATION_JSON_BYTES}); reduce the payload"
        )
        raise HarnessUsageError(message)
    normalized: dict[str, Any] = json.loads(encoded)
    return normalized


def _prepare_invocation(
    inputs: Mapping[str, Any], context: ExecutionContext
) -> tuple[dict[str, Any], ExecutionContext]:
    """Normalize one invocation so both paths deliver identical values.

    Inputs must be a JSON object; both the inputs and the runtime context are
    normalized through one JSON round trip, giving each path an isolated
    JSON-native copy. Non-object inputs would be coerced or rejected
    inconsistently between the two paths, and oversized or non-JSON payloads
    crash the container request encoding with raw ValueErrors while the
    direct path proceeds.
    """

    if not isinstance(inputs, Mapping):
        message = "inputs must be a JSON object (mapping) so both execution paths accept it"
        raise HarnessUsageError(message)
    normalized_inputs = _json_round_trip(inputs, "inputs")
    normalized_runtime = _json_round_trip(context.runtime_context, "runtime context")
    prepared = ExecutionContext(
        invocation_id=context.invocation_id,
        workflow_run_id=context.workflow_run_id,
        runtime_context=normalized_runtime,
        cancellation=context.cancellation,
    )
    return normalized_inputs, prepared


def _require_timeout(timeout_seconds: int) -> None:
    """Reject timeout values outside the container ABI's supported range."""

    if not _MIN_TIMEOUT_SECONDS <= timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        message = (
            f"timeout_seconds must be between {_MIN_TIMEOUT_SECONDS} and "
            f"{_MAX_TIMEOUT_SECONDS}, the container ABI's supported range"
        )
        raise HarnessUsageError(message)

StepTargetOrBinding = StepTarget | StepBinding


@dataclass(frozen=True)
class StepOutcome:
    """One normalized invocation outcome across both SDK execution paths.

    All result assertions in examples and conformance checks go through these
    accessors instead of raw ``StandardOutputWrapper`` fields, so envelope
    changes remain a one-file adapter change inside this package.
    """

    ok: bool
    status: int | None
    status_message: str
    output: Mapping[str, Any] | None
    error: ErrorResult | None
    run_calls: int
    raised: BaseException | None
    transport: Transport
    raw: StandardOutputWrapper | ContainerResult | None


def _resolve_binding(target: StepTargetOrBinding) -> StepBinding:
    """Resolve a binding, step, class, or entry point into one executable binding."""

    if isinstance(target, StepBinding):
        return target
    step = resolve_step(target)
    return installed_binding(step)


def _require_executable(binding: StepBinding) -> None:
    """Reject bindings that lack the SDK's installed-manifest provenance."""

    try:
        validate_executable_step_binding(binding.descriptor, binding.step)
    except StepBindingError as error:
        message = (
            "custom-runner steps execute only through an installed manifest. Bind the "
            "step with step_bindings.bind(step) or, for a compiled workspace, "
            "step_bindings.bind_step(entry_point, workspace=...) so the installed "
            "contract supplies the executable binding; see examples/python-steps."
        )
        raise HarnessUsageError(message) from error


@contextmanager
def _counting_run(step: BaseStep[Any, Any]) -> Iterator[MagicMock]:
    """Count business-hook invocations without changing step behavior.

    The spy patches the instance attribute, so concurrent invocations of the
    same step instance from multiple threads interfere; the helpers are
    sequential by contract.
    """

    with patch.object(step, "run", wraps=step.run) as spy:
        yield spy


def _raised_outcome(
    spy: MagicMock, error: BaseException, transport: Transport
) -> StepOutcome:
    """Normalize an executor or business exception into one failed outcome."""

    return StepOutcome(
        ok=False,
        status=None,
        status_message="",
        output=None,
        error=None,
        run_calls=spy.call_count,
        raised=error,
        transport=transport,
        raw=None,
    )


class _BoundedHandler:
    """Run one container handler on a daemon thread with a bounded wait.

    grpc joins its worker pool at interpreter exit, so a step that ignores
    cooperative cancellation and never returns would keep the whole test
    session alive after its timeout outcome. The handler body runs on a daemon
    thread and the grpc worker waits only until the invocation timeout (plus a
    short grace), so cleanup stays bounded; the daemon thread dies with the
    process.
    """

    _GRACE_SECONDS = 5.0

    def __init__(self, handler: ContainerStepHandler, timeout_seconds: int) -> None:
        self._handler = handler
        self._timeout_seconds = timeout_seconds

    def __call__(self, invocation: ContainerInvocation) -> ContainerResult:
        completed: dict[str, ContainerResult] = {}
        failure: list[BaseException] = []

        def run() -> None:
            try:
                completed["result"] = self._handler(invocation)
            except BaseException as error:  # noqa: BLE001 - re-raised on the worker
                failure.append(error)

        worker = threading.Thread(
            target=run, daemon=True, name="syntara-testing-step-handler"
        )
        worker.start()
        worker.join(self._timeout_seconds + self._GRACE_SECONDS)
        if "result" in completed:
            return completed["result"]
        if failure:
            error = failure[0]
            if isinstance(error, Exception):
                raise error
            # SystemExit and KeyboardInterrupt escape the container service's
            # Exception handlers, which would stall the client until the RPC
            # deadline with a mislabeled timeout; convert them so a terminal
            # failure event is sent promptly.
            raise ContainerInvocationError(
                FailureCode.OUTCOME_UNKNOWN,
                "The plugin step did not complete.",
                outcome="unknown",
            ) from error
        raise ContainerInvocationError(
            FailureCode.TIMEOUT,
            "The plugin step did not complete within the invocation timeout.",
            outcome="unknown",
        )


def invoke_step(
    target: StepTargetOrBinding,
    inputs: Mapping[str, Any],
    *,
    context: ExecutionContext | None = None,
) -> StepOutcome:
    """Execute one bound step in-process through the shared ``StepExecutor``.

    The direct path performs the same input validation, typed business-hook
    invocation, output-schema validation, and ``StandardOutputWrapper``
    construction as platform execution, without a ``ContainerInvocation`` or a
    gRPC server. Executor-level failures (``StepExecutionError``) and business
    exceptions are captured in ``StepOutcome.raised`` so direct and container
    outcomes compare uniformly.

    Args:
        target: A ``StepBinding``, a step class or instance, or a ``module:Class``
            entry point. Custom-runner Step types must arrive as bindings
            produced by ``StepBindingsFactory.bind_step``.
        inputs: Raw input mapping; must be a JSON object encodable within the
            container ABI's 1 MiB limit. Both paths deliver the JSON-normalized
            form (tuples as lists, non-string keys as strings), each through
            its own copy.
        context: Execution context; a locally approved one is built when omitted.
    """

    binding = _resolve_binding(target)
    _require_executable(binding)
    if context is None:
        context = build_execution_context()
    context = normalize_run_id(context)
    inputs, context = _prepare_invocation(inputs, context)
    # Construct outside the catch so harness-usage errors stay loud on both
    # paths instead of being normalized into a quiet failed outcome.
    with _counting_run(binding.step) as spy:
        try:
            wrapper = invoke(binding, inputs, context)
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            # StepExecutionError, business exceptions, and non-Exception
            # BaseExceptions such as SystemExit normalize into ``raised`` so
            # both transports compare uniformly; KeyboardInterrupt stays a
            # genuine control-flow interrupt.
            return _raised_outcome(spy, error, "direct")
    return StepOutcome(
        ok=wrapper.status == 0,
        status=wrapper.status,
        status_message=wrapper.status_message,
        output=wrapper.output,
        error=wrapper.error,
        run_calls=spy.call_count,
        raised=None,
        transport="direct",
        raw=wrapper,
    )


def invoke_step_in_container(
    target: StepTargetOrBinding,
    inputs: Mapping[str, Any],
    *,
    context: ExecutionContext | None = None,
    timeout_seconds: int = 60,
) -> StepOutcome:
    """Execute one bound step over a real in-process container-adapter server.

    The adapter path runs the same registered implementation, validation, and
    result wrapping as the direct path, then carries the terminal result over
    the container gRPC ABI. The server and channel are process-local
    (``127.0.0.1``); authors never construct a ``ContainerInvocation``.

    A preset ``cancelled`` context is a direct-path feature: the container
    path delivers cooperative cancellation through the transport, so a
    pre-cancelled context is not transported and the handler will not observe
    it. Test pre-cancelled contexts with :func:`invoke_step`.

    Business results above the container ABI's inline bound (1 MiB) are also
    a direct-path concern: the harness wires no ``ResultObjectStore``, so an
    oversized result that succeeds on the direct path fails here with
    ``ABI_VIOLATION``. Test large outputs with :func:`invoke_step`.

    Args:
        target: A ``StepBinding``, a step class or instance, or a ``module:Class``
            entry point, with the same Action rule as :func:`invoke_step`.
        inputs: Raw input mapping; must be a JSON object encodable within the
            container ABI's 1 MiB limit. Both paths deliver the JSON-normalized
            form, each through its own copy.
        context: Execution context supplying identifiers and runtime context.
        timeout_seconds: Container invocation timeout in seconds, within the
            ABI's supported 1..86400 range; a step that ignores cancellation
            cannot block the test session beyond this timeout plus a short
            grace.
    """

    binding = _resolve_binding(target)
    _require_executable(binding)
    _require_timeout(timeout_seconds)
    if context is None:
        context = build_execution_context()
    context = normalize_run_id(context)
    inputs, context = _prepare_invocation(inputs, context)
    bounded_handler = _BoundedHandler(
        container_step_for_binding(binding), timeout_seconds
    )
    registry = StepRegistry({binding.descriptor.step_identity: bounded_handler})
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, registry)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = ContainerClient(channel)
        # normalize_run_id guarantees a run id; narrow the optional type for
        # the container request, which requires a str.
        workflow_run_id = context.workflow_run_id
        assert workflow_run_id is not None
        invocation = ContainerInvocation(
            invocation_id=context.invocation_id,
            workflow_run_id=workflow_run_id,
            step_revision_id=binding.descriptor.step_identity,
            inputs=inputs,
            credential_file_paths={},
            timeout_seconds=timeout_seconds,
            runtime_context=context.runtime_context,
        )
        with _counting_run(binding.step) as spy:
            try:
                result = client.execute(invocation)
            except ContainerRemoteError as remote_error:
                return _raised_outcome(spy, remote_error, "container")
        wrapper_error = (
            ErrorResult.model_validate(result.error_result)
            if result.error_result is not None else None
        )
        return StepOutcome(
            ok=result.status_code == 0,
            status=result.status_code,
            status_message=result.status_message,
            output=result.output,
            error=wrapper_error,
            run_calls=spy.call_count,
            raised=None,
            transport="container",
            raw=result,
        )
    finally:
        channel.close()
        server.stop(None)
