"""Composable conformance checks for bound plugin steps."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from syntara_plugin.runtime import FailureCode, StepBinding

from .context_builder import build_execution_context
from .equivalence import assert_outcomes_equivalent
from .invocation import invoke_step, invoke_step_in_container
from .payloads import Expectation, PayloadCase

if TYPE_CHECKING:
    from syntara_plugin.runtime import ExecutionContext

Case = PayloadCase | Mapping[str, Any]


@dataclass(frozen=True)
class ConformanceFailure:
    """One property violation found for one payload case."""

    case: str
    check: str
    detail: str


@dataclass(frozen=True)
class ConformanceReport:
    """The merged result of one conformance run."""

    failures: tuple[ConformanceFailure, ...] = ()

    @property
    def passed(self) -> bool:
        """Whether every checked property held for every case."""

        return not self.failures


def _normalize(case: Case, index: int) -> PayloadCase:
    if isinstance(case, PayloadCase):
        return case
    return PayloadCase(name=f"case-{index}", inputs=case, expectation=Expectation.VALID)


def check_invalid_blocks_hook(
    binding: StepBinding,
    invalid_cases: Sequence[Case],
    *,
    context: ExecutionContext | None = None,
) -> ConformanceReport:
    """Verify every invalid case fails validation before the business hook runs."""

    failures: list[ConformanceFailure] = []
    for index, case in enumerate(invalid_cases):
        payload = _normalize(case, index)
        outcome = invoke_step(binding, payload.inputs, context=context)
        problems: list[str] = []
        if outcome.run_calls != 0:
            problems.append(f"business hook ran {outcome.run_calls} times")
        if outcome.raised is not None or outcome.ok or outcome.error is None:
            problems.append(
                f"expected an INVALID_INPUT failure, got ok={outcome.ok}, "
                f"error={outcome.error}, raised={outcome.raised}"
            )
        elif outcome.error.code != FailureCode.INVALID_INPUT.value:
            problems.append(f"expected INVALID_INPUT code, got {outcome.error.code}")
        if problems:
            failures.append(
                ConformanceFailure(payload.name, "invalid_blocks_hook", "; ".join(problems))
            )
    return ConformanceReport(tuple(failures))


def check_valid_reaches_hook_once(
    binding: StepBinding,
    valid_cases: Sequence[Case],
    *,
    context: ExecutionContext | None = None,
) -> ConformanceReport:
    """Verify every valid case reaches the business hook exactly once and succeeds."""

    failures: list[ConformanceFailure] = []
    for index, case in enumerate(valid_cases):
        payload = _normalize(case, index)
        outcome = invoke_step(binding, payload.inputs, context=context)
        problems: list[str] = []
        if outcome.raised is not None:
            problems.append(f"raised {outcome.raised!r}")
        if not outcome.ok:
            problems.append(f"ok={outcome.ok}, error={outcome.error}")
        if outcome.run_calls != 1:
            problems.append(f"business hook ran {outcome.run_calls} times, expected 1")
        if problems:
            failures.append(
                ConformanceFailure(payload.name, "valid_reaches_hook_once", "; ".join(problems))
            )
    return ConformanceReport(tuple(failures))


def check_paths_equivalent(
    binding: StepBinding,
    cases: Sequence[Case],
    *,
    context: ExecutionContext | None = None,
) -> ConformanceReport:
    """Verify the direct and container-adapter paths yield equivalent outcomes.

    Both paths execute each case under one shared context: when ``context`` is
    omitted, a fresh context is built per case and passed to both invocations,
    so steps whose output correlates with the invocation identifiers compare
    the same identifiers on both paths.
    """

    failures: list[ConformanceFailure] = []
    for index, case in enumerate(cases):
        payload = _normalize(case, index)
        case_context = context or build_execution_context()
        direct = invoke_step(binding, payload.inputs, context=case_context)
        container = invoke_step_in_container(
            binding, payload.inputs, context=case_context
        )
        try:
            assert_outcomes_equivalent(direct, container)
        except AssertionError as error:
            failures.append(
                ConformanceFailure(payload.name, "paths_equivalent", str(error))
            )
    return ConformanceReport(tuple(failures))


def check_step_conformance(
    binding: StepBinding,
    *,
    valid_cases: Sequence[Case] = (),
    invalid_cases: Sequence[Case] = (),
    context: ExecutionContext | None = None,
    include_equivalence: bool = True,
) -> ConformanceReport:
    """Run the standard conformance properties for one bound step.

    Checks that invalid input blocks the business hook, valid input reaches it
    exactly once, and (unless ``include_equivalence=False``) that the direct
    and container-adapter SDK paths yield equivalent outcomes for the valid
    cases. A run with no cases passes vacuously; always pass at least one
    valid and one invalid case for a meaningful report.
    """

    reports = [
        check_invalid_blocks_hook(binding, invalid_cases, context=context),
        check_valid_reaches_hook_once(binding, valid_cases, context=context),
    ]
    if include_equivalence and valid_cases:
        reports.append(check_paths_equivalent(binding, valid_cases, context=context))
    failures = tuple(
        failure for report in reports for failure in report.failures
    )
    return ConformanceReport(failures)
