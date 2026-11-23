"""OpenTelemetry instrumentation for the shared SDK execution boundary."""

import logging
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from time import perf_counter_ns, thread_time_ns
from typing import Literal

from opentelemetry import metrics, trace

_SDK_SCOPE = "syntara_plugin.runtime"
_MODE_ENV = "SYNTARA_RUNTIME_TELEMETRY_MODE"
_NOOP_STAGE = nullcontext()
_NANOSECONDS_PER_SECOND = 1_000_000_000
_DURATION_BUCKETS = (
    0.00001,
    0.000025,
    0.00005,
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1,
    2.5,
    5,
    10,
    30,
)
_Stage = Literal[
    "invocation",
    "input_validation",
    "step_execution",
    "output_serialization",
    "output_validation",
    "wrapper",
    "credential_scope",
    "credential_output_check",
    "container_invocation",
    "container_result",
]


@dataclass
class _StageResult:
    failed: bool = False


class _RuntimeTelemetry:
    def __init__(self, meter: metrics.Meter, tracer: trace.Tracer, *, detailed: bool) -> None:
        self.detailed = detailed
        self._tracer = tracer
        self._duration = meter.create_histogram(
            "syntara.sdk.runtime.stage.duration",
            unit="s",
            description="Wall time of a Syntara SDK runtime stage.",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        )
        self._cpu_time = (
            meter.create_histogram(
                "syntara.sdk.runtime.invocation.cpu_time",
                unit="s",
                description="Thread CPU time of a Syntara SDK invocation.",
                explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
            )
            if detailed
            else None
        )

    @contextmanager
    def measure(self, stage: _Stage) -> Iterator[_StageResult]:
        result = _StageResult()
        span = None
        with suppress(Exception):
            active_context = trace.get_current_span().get_span_context()
            candidate = self._tracer.start_span(f"syntara.sdk.runtime.{stage}")
            # A no-op tracer can return a non-recording copy of the caller's
            # span. Preserve the active caller instead of replacing its span.
            if candidate.get_span_context() != active_context:
                span = candidate

        # Activating the span also lets nested stages inherit a caller's trace.
        # Strip automatic exception events and descriptions on every exit.
        span_scope = (
            trace.use_span(span, record_exception=False, set_status_on_exception=False)
            if span is not None
            else nullcontext()
        )
        try:
            with span_scope:
                cpu_time = self._cpu_time
                cpu_start = (
                    thread_time_ns() if stage == "invocation" and cpu_time is not None else None
                )
                start = perf_counter_ns()
                try:
                    yield result
                except BaseException:
                    result.failed = True
                    raise
                finally:
                    elapsed = (perf_counter_ns() - start) / _NANOSECONDS_PER_SECOND
                    cpu_elapsed = (
                        (thread_time_ns() - cpu_start) / _NANOSECONDS_PER_SECOND
                        if cpu_start is not None
                        else None
                    )
                    outcome = "error" if result.failed else "ok"
                    # A failed telemetry provider must not change the runtime result.
                    with suppress(Exception):
                        self._duration.record(elapsed, {"stage": stage, "outcome": outcome})
                        if cpu_elapsed is not None and cpu_time is not None:
                            cpu_time.record(cpu_elapsed, {"outcome": outcome})
                    if span is not None:
                        with suppress(Exception):
                            span.set_attribute("syntara.sdk.outcome", outcome)
                            if result.failed:
                                span.set_status(trace.StatusCode.ERROR)
        finally:
            if span is not None:
                with suppress(Exception):
                    span.end()


def _configured_runtime_telemetry() -> _RuntimeTelemetry | None:
    mode = os.environ.get(_MODE_ENV, "off")
    if mode == "off":
        return None
    if mode not in {"production", "performance"}:
        logging.getLogger(__name__).warning(
            "Ignoring invalid SDK telemetry mode. Use off, production, or performance."
        )
        return None
    detailed = mode == "performance"
    tracer = trace.NoOpTracerProvider().get_tracer(_SDK_SCOPE)
    with suppress(Exception):
        tracer = trace.get_tracer(_SDK_SCOPE)
    try:
        return _RuntimeTelemetry(metrics.get_meter(_SDK_SCOPE), tracer, detailed=detailed)
    except Exception:
        return _RuntimeTelemetry(
            metrics.NoOpMeterProvider().get_meter(_SDK_SCOPE), tracer, detailed=detailed
        )


runtime_telemetry = _configured_runtime_telemetry()


def measure_detail(stage: _Stage) -> AbstractContextManager[_StageResult | None]:
    """Return a shared no-op scope when detailed telemetry is disabled."""
    telemetry = runtime_telemetry
    return telemetry.measure(stage) if telemetry is not None and telemetry.detailed else _NOOP_STAGE
