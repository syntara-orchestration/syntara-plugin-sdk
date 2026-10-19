"""Tests for the sample payload library and secret redaction."""

from __future__ import annotations

import logging

import pytest
from syntara_plugin.runtime import FailureCode, StepExecutionError
from syntara_plugin.runtime.container import ContainerInvocationError
from syntara_plugin.testing import (
    DELIBERATE_FAILURE_CASES,
    FIXED_SECRET_SENTINEL,
    INVALID_INPUT_CASES,
    INVALID_OUTPUT_CASES,
    PAYLOAD_CASES,
    VALID_INPUT_CASES,
    Expectation,
    assert_no_secrets,
    invoke_step,
    secret_sentinel,
)
from syntara_plugin.testing.payloads import (
    FailingReferenceStep,
    ReferenceTaskStep,
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
        outcome = invoke_step(ReferenceTaskStep(), case.inputs)
        assert outcome.ok, case.name
        assert outcome.run_calls == 1, case.name


def test_invalid_cases_fail_validation_against_the_reference_step() -> None:
    for case in INVALID_INPUT_CASES:
        outcome = invoke_step(ReferenceTaskStep(), case.inputs)
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
    outcome = invoke_step(ReferenceTaskStep(), {"title": "clean"})
    assert_no_secrets(outcome, [secret_sentinel(), secret_sentinel()])


def test_assert_no_secrets_fails_when_output_contains_the_sentinel() -> None:
    token = secret_sentinel()
    outcome = invoke_step(ReferenceTaskStep(), {"title": token})
    with pytest.raises(pytest.fail.Exception, match="leaked"):
        assert_no_secrets(outcome, [token])


def test_assert_no_secrets_fails_when_the_error_message_contains_the_sentinel() -> None:
    token = secret_sentinel()

    class LeakyStep(ReferenceTaskStep):
        def run(self, inputs: object, context: object) -> object:
            raise ContainerInvocationError(
                FailureCode.UPSTREAM_TRANSIENT, f"failed for {token}"
            )

    outcome = invoke_step(LeakyStep(), {"title": "x"})
    with pytest.raises(pytest.fail.Exception, match="leaked"):
        assert_no_secrets(outcome, [token])


def test_assert_no_secrets_scans_captured_logs(caplog: pytest.LogCaptureFixture) -> None:
    token = secret_sentinel()
    with caplog.at_level(logging.INFO):
        logging.getLogger("syntara-plugin-testing-test").info("handling %s", token)
    with pytest.raises(pytest.fail.Exception, match="leaked"):
        assert_no_secrets("clean text", [token], caplog=caplog)
    assert_no_secrets("clean text", [secret_sentinel()], caplog=caplog)


def test_assert_no_secrets_accepts_plain_text() -> None:
    assert_no_secrets("a clean message", [secret_sentinel()])
    with pytest.raises(pytest.fail.Exception):
        assert_no_secrets(f"a message with {FIXED_SECRET_SENTINEL} inside", [FIXED_SECRET_SENTINEL])
