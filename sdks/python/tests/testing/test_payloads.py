"""Tests for the sample payload library and secret redaction."""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ControlStep,
    ExecutionContext,
    FailureCode,
    StepExecutionError,
)
from syntara_plugin.runtime.container import ContainerInvocationError
from syntara_plugin.testing import (
    DELIBERATE_FAILURE_CASES,
    FIXED_SECRET_SENTINEL,
    INVALID_INPUT_CASES,
    INVALID_OUTPUT_CASES,
    PAYLOAD_CASES,
    VALID_INPUT_CASES,
    Expectation,
    HarnessUsageError,
    assert_no_secrets,
    build_execution_context,
    invoke_step,
    secret_sentinel,
)
from syntara_plugin.testing.payloads import (
    FailingReferenceStep,
    ReferenceControlStep,
    schema_violating_binding,
)


def test_payload_cases_cover_every_ticket_category() -> None:
    expectations = {case.expectation for case in PAYLOAD_CASES}
    assert expectations == {
        Expectation.VALID,
        Expectation.INVALID_INPUT,
        Expectation.DELIBERATE_FAILURE,
        Expectation.INVALID_OUTPUT,
    }
    assert len(PAYLOAD_CASES) == (
        len(VALID_INPUT_CASES)
        + len(INVALID_INPUT_CASES)
        + len(DELIBERATE_FAILURE_CASES)
        + len(INVALID_OUTPUT_CASES)
    )
    assert FIXED_SECRET_SENTINEL in {
        case.inputs.get("api_token")
        for case in VALID_INPUT_CASES
        if "api_token" in case.inputs
    }


def test_valid_cases_succeed_against_the_reference_step() -> None:
    for case in VALID_INPUT_CASES:
        outcome = invoke_step(ReferenceControlStep(), case.inputs)
        assert outcome.ok, case.name
        assert outcome.run_calls == 1, case.name


def test_invalid_cases_fail_validation_against_the_reference_step() -> None:
    for case in INVALID_INPUT_CASES:
        outcome = invoke_step(ReferenceControlStep(), case.inputs)
        assert not outcome.ok, case.name
        assert outcome.run_calls == 0, case.name


def test_deliberate_failure_case_matches_the_failing_reference_step() -> None:
    for case in DELIBERATE_FAILURE_CASES:
        outcome = invoke_step(FailingReferenceStep(), case.inputs)
        assert isinstance(outcome.raised, ContainerInvocationError), case.name


def test_invalid_output_case_matches_the_schema_violating_binding() -> None:
    for case in INVALID_OUTPUT_CASES:
        outcome = invoke_step(schema_violating_binding(), case.inputs)
        assert isinstance(outcome.raised, StepExecutionError), case.name


def test_secret_sentinels_are_unique() -> None:
    sentinels = {secret_sentinel() for _ in range(50)}
    assert len(sentinels) == 50


def test_assert_no_secrets_passes_on_a_clean_outcome() -> None:
    outcome = invoke_step(ReferenceControlStep(), {"title": "clean"})
    assert_no_secrets(outcome, [secret_sentinel(), secret_sentinel()])


def test_assert_no_secrets_fails_when_output_contains_the_sentinel() -> None:
    token = secret_sentinel()
    outcome = invoke_step(ReferenceControlStep(), {"title": token})
    with pytest.raises(AssertionError, match="leaked"):
        assert_no_secrets(outcome, [token])


def test_leak_report_does_not_echo_the_sentinel_value() -> None:
    token = secret_sentinel()
    outcome = invoke_step(ReferenceControlStep(), {"title": token})
    with pytest.raises(AssertionError) as raised:
        assert_no_secrets(outcome, [token])
    message = str(raised.value)
    assert token not in message
    assert "sentinel #0" in message
    assert "business output" in message


class _LeakyMessageStep(ReferenceControlStep):
    """Deliberately fails with the token in its safe message."""

    token: str = ""

    def run(self, inputs: object, context: object) -> object:
        raise ContainerInvocationError(
            FailureCode.UPSTREAM_TRANSIENT, f"failed for {self.token}"
        )


def test_assert_no_secrets_fails_when_the_error_message_contains_the_sentinel() -> None:
    token = secret_sentinel()
    step = _LeakyMessageStep()
    step.token = token
    outcome = invoke_step(step, {"title": "x"})
    with pytest.raises(AssertionError, match="leaked"):
        assert_no_secrets(outcome, [token])


def test_assert_no_secrets_scans_captured_logs(caplog: pytest.LogCaptureFixture) -> None:
    token = secret_sentinel()
    with caplog.at_level(logging.INFO):
        logging.getLogger("syntara-plugin-testing-test").info("handling %s", token)
    with pytest.raises(AssertionError, match="leaked"):
        assert_no_secrets("clean text", [token], caplog=caplog)
    assert_no_secrets("clean text", [secret_sentinel()], caplog=caplog)


def test_assert_no_secrets_accepts_plain_text() -> None:
    assert_no_secrets("a clean message", [secret_sentinel()])
    with pytest.raises(AssertionError):
        assert_no_secrets(f"a message with {FIXED_SECRET_SENTINEL} inside", [FIXED_SECRET_SENTINEL])


class _ChainInput(BaseModel):
    """A permissive input for the exception-chain redaction steps."""

    model_config = {"extra": "allow"}
    title: str = "x"


class _ChainOutput(BaseModel):
    summary: str = "y"


class _CauseStep(ControlStep[_ChainInput, _ChainOutput]):
    """Hides the token in the explicit ``__cause__`` chain."""

    token: str = ""

    def run(self, inputs: _ChainInput, context: ExecutionContext) -> _ChainOutput:
        try:
            raise ValueError(self.token)
        except ValueError as error:
            raise RuntimeError("safe failure") from error


class _ContextStep(ControlStep[_ChainInput, _ChainOutput]):
    """Hides the token in the implicit ``__context__`` chain."""

    token: str = ""

    def run(self, inputs: _ChainInput, context: ExecutionContext) -> _ChainOutput:
        try:
            raise ValueError(self.token)
        except ValueError:
            # ``from None`` suppresses the context from display but still
            # records it on ``__context__``, which the scan must walk.
            raise RuntimeError("safe failure") from None


class _NotesStep(ControlStep[_ChainInput, _ChainOutput]):
    """Hides the token in ``__notes__``."""

    token: str = ""

    def run(self, inputs: _ChainInput, context: ExecutionContext) -> _ChainOutput:
        error = RuntimeError("safe failure")
        error.add_note(f"token {self.token}")
        raise error


class _OpaqueError(RuntimeError):
    """A custom ``__repr__`` that discards its payload."""

    def __repr__(self) -> str:
        return "OpaqueError(<hidden>)"


class _OpaqueArgsStep(ControlStep[_ChainInput, _ChainOutput]):
    """Hides the token in args behind an opaque ``__repr__``."""

    token: str = ""

    def run(self, inputs: _ChainInput, context: ExecutionContext) -> _ChainOutput:
        raise _OpaqueError("safe failure", self.token)


def test_assert_no_secrets_scans_exception_chains() -> None:
    """Secrets hidden in an exception's chain, args, or notes are caught."""

    token = secret_sentinel()
    context = build_execution_context(invocation_id="chain-redaction")
    for step_class in (_CauseStep, _ContextStep, _NotesStep, _OpaqueArgsStep):
        step = step_class()
        step.token = token
        outcome = invoke_step(step, {}, context=context)
        with pytest.raises(AssertionError, match="leaked") as raised:
            assert_no_secrets(outcome, [token])
        assert token not in str(raised.value)


def test_function_local_step_classes_are_rejected_with_guidance() -> None:
    """The installed-step contract requires importable entry points."""

    class LocalStep(ControlStep[_ChainInput, _ChainOutput]):
        def run(self, inputs: _ChainInput, context: ExecutionContext) -> _ChainOutput:
            return _ChainOutput(summary="unreachable")

    context = build_execution_context(invocation_id="local-class")
    step = LocalStep()
    with pytest.raises(HarnessUsageError, match="module level"):
        invoke_step(step, {}, context=context)
