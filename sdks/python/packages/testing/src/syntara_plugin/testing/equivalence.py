"""Equivalence assertions between the direct and container execution paths."""

from __future__ import annotations

from typing import Any

from .invocation import StepOutcome


def _failure_code(outcome: StepOutcome) -> Any:
    raised = outcome.raised
    return getattr(raised, "code", None) if raised is not None else None


def assert_outcomes_equivalent(direct: StepOutcome, container: StepOutcome) -> None:
    """Assert two outcomes of one invocation agree on the public contract.

    Compares success, status, business output, and error envelope. Schema
    digests are deliberately ignored: installed and provisional digests
    legitimately differ between the two transports. Executor-level failures are
    equivalent when both paths report the invocation ended in a raised failure;
    failure codes are compared when both paths carry one.
    """

    if direct.transport != "direct" or container.transport != "container":
        message = (
            "assert_outcomes_equivalent expects the direct outcome first, then the container "
            "outcome"
        )
        raise AssertionError(message)
    problems: list[str] = []
    if (direct.raised is None) != (container.raised is None):
        problems.append(f"raised: direct={direct.raised!r} container={container.raised!r}")
    if direct.raised is not None and container.raised is not None:
        direct_code = _failure_code(direct)
        container_code = _failure_code(container)
        if direct_code is not None and container_code is not None and direct_code != container_code:
            problems.append(f"failure code: direct={direct_code} container={container_code}")
    else:
        if direct.ok != container.ok:
            problems.append(f"ok: direct={direct.ok} container={container.ok}")
        if direct.status != container.status:
            problems.append(f"status: direct={direct.status} container={container.status}")
        if direct.output != container.output:
            problems.append(f"output: direct={direct.output!r} container={container.output!r}")
        if direct.error != container.error:
            problems.append(f"error: direct={direct.error!r} container={container.error!r}")
    if direct.run_calls != container.run_calls:
        problems.append(f"run_calls: direct={direct.run_calls} container={container.run_calls}")
    if problems:
        details = "; ".join(problems)
        raise AssertionError(f"direct and container outcomes differ: {details}")
