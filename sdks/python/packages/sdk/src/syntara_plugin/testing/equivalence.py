"""Equivalence assertions between the direct and container execution paths."""

from __future__ import annotations

from collections.abc import Mapping

from syntara_plugin.runtime import ErrorResult, FailureCode, StepExecutionError
from syntara_plugin.runtime.container import ContainerInvocationError, ContainerRemoteError

from .invocation import StepOutcome


def _failure_category(outcome: StepOutcome) -> FailureCode | None:
    """Map one raised outcome onto the public failure category both paths present.

    The mapping mirrors the runtime's own conversions: a deliberate
    ``ContainerInvocationError`` keeps its category, an executor output
    violation becomes an ABI violation on the container path, and any other
    exception becomes OUTCOME_UNKNOWN, exactly as the container service
    reports unhandled step failures.
    """

    raised = outcome.raised
    if raised is None:
        return None
    if isinstance(raised, ContainerInvocationError | ContainerRemoteError):
        return raised.code
    if isinstance(raised, StepExecutionError):
        return FailureCode.ABI_VIOLATION
    return FailureCode.OUTCOME_UNKNOWN


def _describe(value: object) -> str:
    """Describe one compared value by type and size, never by content.

    Equivalence failure messages must not echo field values: a differing
    output or message may carry a secret sentinel, and the assertion text
    reaches CI logs.
    """

    if value is None:
        return "none"
    if isinstance(value, Mapping):
        return f"{type(value).__name__} with {len(value)} keys"
    if isinstance(value, str):
        return f"str of {len(value)} chars"
    return type(value).__name__


def _error_brief(error: ErrorResult | None) -> str:
    """Describe an error envelope by category only, never its message."""

    return "none" if error is None else f"code {error.code}"


def assert_outcomes_equivalent(direct: StepOutcome, container: StepOutcome) -> None:
    """Assert two outcomes of one invocation agree on the public contract.

    Compares success, status, status message, business output, and error
    envelope. Schema digests are deliberately ignored: installed and
    provisional digests legitimately differ between the two transports. When
    both paths report a raised failure, the failure categories must match;
    direct-path exceptions are normalized onto the same category model the
    container path reports.

    Failure messages identify the differing field with type and size metadata
    only, never raw values, so a secret-bearing field cannot leak through the
    assertion text into CI logs.

    Equivalence holds within the container ABI's inline result bound (1 MiB):
    above it the container path fails with ``ABI_VIOLATION`` because the
    harness wires no object store, so compare oversized business results on
    the direct path only.
    """

    if direct.transport != "direct" or container.transport != "container":
        message = (
            "assert_outcomes_equivalent expects the direct outcome first, then the "
            "container outcome"
        )
        raise AssertionError(message)
    problems: list[str] = []
    if (direct.raised is None) != (container.raised is None):
        problems.append(
            f"raised differs: direct={_describe(direct.raised)} "
            f"container={_describe(container.raised)}"
        )
    if direct.raised is not None and container.raised is not None:
        direct_category = _failure_category(direct)
        container_category = _failure_category(container)
        if direct_category != container_category:
            problems.append(
                f"failure category: direct={direct_category} container={container_category}"
            )
    else:
        if direct.ok != container.ok:
            problems.append(f"ok: direct={direct.ok} container={container.ok}")
        if direct.status != container.status:
            problems.append(f"status: direct={direct.status} container={container.status}")
        if direct.status_message != container.status_message:
            problems.append(
                f"status_message differs: direct=({_describe(direct.status_message)}) "
                f"container=({_describe(container.status_message)})"
            )
        if direct.output != container.output:
            problems.append(
                f"output differs: direct=({_describe(direct.output)}) "
                f"container=({_describe(container.output)})"
            )
        direct_error = direct.error.model_dump(mode="json") if direct.error is not None else None
        container_error = (
            container.error.model_dump(mode="json") if container.error is not None else None
        )
        if direct_error != container_error:
            problems.append(
                f"error differs: direct={_error_brief(direct.error)} "
                f"container={_error_brief(container.error)}"
            )
    if direct.run_calls != container.run_calls:
        problems.append(f"run_calls: direct={direct.run_calls} container={container.run_calls}")
    if problems:
        details = "; ".join(problems)
        raise AssertionError(f"direct and container outcomes differ: {details}")
