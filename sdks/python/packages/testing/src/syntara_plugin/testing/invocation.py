"""Direct and container-adapter invocation of bound steps for local tests."""

from __future__ import annotations

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
    ErrorMessage,
    FailureCode,
    StandardOutputWrapper,
    StepBinding,
    StepDescriptor,
    StepExecutor,
    StepTaxonomy,
)
from syntara_plugin.runtime.adapter import container_step_for_binding
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

from .context_builder import build_execution_context, normalize_run_id
from .errors import HarnessUsageError
from .loader import StepTarget, default_step_identity, resolve_step

if TYPE_CHECKING:
    from collections.abc import Iterator

    from syntara_plugin.runtime import ExecutionContext

Transport = Literal["direct", "container"]

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
    error: ErrorMessage | None
    run_calls: int
    raised: BaseException | None
    transport: Transport
    raw: StandardOutputWrapper | ContainerResult | None


def _resolve_binding(target: StepTargetOrBinding) -> StepBinding:
    """Resolve a binding, step, class, or entry point into one executable binding."""

    if isinstance(target, StepBinding):
        return target
    step = resolve_step(target)
    return StepBinding(StepDescriptor.for_step(default_step_identity(step), step), step)


def _require_executable(binding: StepBinding) -> None:
    """Reject Action bindings that lack the SDK's installed-manifest provenance."""

    is_action = binding.descriptor.taxonomy is StepTaxonomy.ACTION
    if is_action and not binding.descriptor.is_installed_action:
        message = (
            "Action steps execute only through an installed manifest. Bind the step with "
            "step_bindings.bind_action(entry_point, workspace=...) so the compiled manifest "
            "supplies the executable contract; see examples/python-steps."
        )
        raise HarnessUsageError(message)


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
        completed: dict[str, ContainerResult | BaseException] = {}

        def run() -> None:
            try:
                completed["result"] = self._handler(invocation)
            except BaseException as error:  # noqa: BLE001 - re-raised on the worker
                completed["error"] = error

        worker = threading.Thread(
            target=run, daemon=True, name="syntara-testing-step-handler"
        )
        worker.start()
        worker.join(self._timeout_seconds + self._GRACE_SECONDS)
        if "result" in completed:
            return completed["result"]  # type: ignore[return-value]
        if "error" in completed:
            raise completed["error"]
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
            entry point. Action steps must arrive as bindings produced by
            ``StepBindingsFactory.bind_action``.
        inputs: Raw JSON-style input mapping.
        context: Execution context; a locally approved one is built when omitted.
    """

    binding = _resolve_binding(target)
    _require_executable(binding)
    if context is None:
        context = build_execution_context()
    context = normalize_run_id(context)
    # Construct outside the catch so harness-usage errors stay loud on both
    # paths instead of being normalized into a quiet failed outcome.
    executor = StepExecutor(binding)
    with _counting_run(binding.step) as spy:
        try:
            wrapper = executor.execute(inputs, context)
        except Exception as error:
            # StepExecutionError and business exceptions normalize into
            # ``raised`` so both transports compare uniformly.
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

    Args:
        target: A ``StepBinding``, a step class or instance, or a ``module:Class``
            entry point, with the same Action rule as :func:`invoke_step`.
        inputs: Raw JSON-style input mapping.
        context: Execution context supplying identifiers and runtime context.
        timeout_seconds: Container invocation timeout in seconds; a step that
            ignores cancellation cannot block the test session beyond this
            timeout plus a short grace.
    """

    binding = _resolve_binding(target)
    _require_executable(binding)
    if context is None:
        context = build_execution_context()
    context = normalize_run_id(context)
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
        invocation = ContainerInvocation(
            invocation_id=context.invocation_id,
            workflow_run_id=context.workflow_run_id,
            step_revision_id=binding.descriptor.step_identity,
            inputs=dict(inputs),
            credential_file_paths={},
            timeout_seconds=timeout_seconds,
            runtime_context=dict(context.runtime_context),
        )
        with _counting_run(binding.step) as spy:
            try:
                result = client.execute(invocation)
            except ContainerRemoteError as error:
                return _raised_outcome(spy, error, "container")
        error = None
        if result.error_code:
            error = ErrorMessage(
                code=result.error_code,
                message=result.error_message,
                retryable=result.retryable,
            )
        return StepOutcome(
            ok=result.status_code == 0,
            status=result.status_code,
            status_message=result.status_message,
            output=result.output,
            error=error,
            run_calls=spy.call_count,
            raised=None,
            transport="container",
            raw=result,
        )
    finally:
        channel.close()
        server.stop(None)
