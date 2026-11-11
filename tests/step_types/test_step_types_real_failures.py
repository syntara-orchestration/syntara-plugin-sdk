"""Classifier tests over real ``syntara-plugin run --json`` failure documents.

The fixtures were captured from actual runs of the pinned step types with
failing inputs (and one deliberately crashing step), then had machine-specific
paths and random IDs replaced. They pin the classifier to the document shapes
the SDK really emits, not to strings written for the test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from classifier import Failure, classify

FIXTURES = Path(__file__).parent / "fixtures"
MAKE_ERROR = "make[1]: *** [Makefile:75: sdk-plugin-run] Error 1\n"


def document(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def run_failure(plugin: str, diagnostic: dict) -> Failure:
    # The make target only prints jq's verdict; the analysis reads the diagnostic run.
    return Failure(
        stage="run",
        plugin=plugin,
        exit_code=2,
        stdout="false\n",
        stderr=MAKE_ERROR,
        diagnostic=diagnostic,
        step_types_dir=Path("/work/step-types"),
    )


@pytest.mark.parametrize(
    ("fixture", "plugin", "category", "rule"),
    [
        ("run-policy-denied", "http-request", "implementation", "IM003"),
        ("run-script-exit", "script-python", "implementation", "IM003"),
        ("run-upstream-503", "http-request", "infra", "IN004"),
        ("run-invalid-input", "http-request", "contract", "CT006"),
        ("run-step-crash", "http-request", "implementation", "IM002"),
    ],
)
def test_real_failure_documents(fixture: str, plugin: str, category: str, rule: str) -> None:
    result = classify(run_failure(plugin, document(fixture)))
    assert (result.category, result.rule_id) == (category, rule)


def test_step_reported_failure_quotes_the_error_code() -> None:
    result = classify(run_failure("http-request", document("run-policy-denied")))
    assert result.confidence == "deterministic"
    assert result.evidence == (
        "error.code POLICY_DENIED: URL targets a blocked address: 127.0.0.1",
    )


def test_upstream_503_is_retryable_infra() -> None:
    result = classify(run_failure("http-request", document("run-upstream-503")))
    assert result.facts == {"code": "UPSTREAM_TRANSIENT", "retryable": True}
    assert result.actions == ("Re-run the failed job.",)


def test_step_crash_points_at_the_step_types_frame_and_exception() -> None:
    result = classify(run_failure("http-request", document("run-step-crash")))
    assert result.evidence[0].startswith("/work/step-types/http-request/src/")
    assert "KeyError: 'injected-step-bug'" in result.evidence
    assert "error.code OUTCOME_UNKNOWN: The step did not complete." in result.evidence
    assert result.frames[0].origin == "step-types"


def test_crash_inside_the_sdk_is_an_sdk_regression() -> None:
    crash = document("run-step-crash")
    crash["errorDetail"] = (
        "Traceback (most recent call last):\n"
        '  File "/work/step-types/http-request/src/syntara_node_http_request/__init__.py", '
        "line 43, in run\n"
        "    return context.emit(x)\n"
        '  File "/work/step-types/.venv/lib/python3.12/site-packages/syntara_plugin/runtime/'
        'context.py", line 9, in emit\n'
        "    raise AttributeError('gone')\n"
        "AttributeError: gone\n"
    )
    result = classify(run_failure("http-request", crash))
    assert (result.category, result.rule_id) == ("sdk-regression", "SR001")
    assert "AttributeError: gone" in result.evidence


def test_invalid_input_quotes_the_schema_detail() -> None:
    result = classify(run_failure("http-request", document("run-invalid-input")))
    assert "inputs/method: enum constraint is not satisfied" in result.evidence


def test_network_flavoured_sdk_crash_is_still_an_sdk_regression() -> None:
    # A traceback proves the side even when the exception text looks like infra.
    crash = document("run-step-crash")
    crash["errorDetail"] = (
        "Traceback (most recent call last):\n"
        '  File "/work/step-types/http-request/src/syntara_node_http_request/__init__.py", '
        "line 40, in run\n"
        "    client = context.http_client()\n"
        '  File "/work/step-types/.venv/lib/python3.12/site-packages/syntara_plugin/runtime/'
        'context.py", line 9, in http_client\n'
        "    raise httpx.ConnectError('refused')\n"
        "httpx.ConnectError: refused\n"
    )
    result = classify(run_failure("http-request", crash))
    assert (result.category, result.rule_id) == ("sdk-regression", "SR001")


def test_validate_compiler_diagnostic_is_classified_by_its_code() -> None:
    # validate --json | jq -e '.ok == true' leaves only "false"; the diagnostic
    # re-run captured the real compiler document.
    failure = Failure(
        stage="validate",
        plugin="http-request",
        exit_code=2,
        stdout="false\n",
        stderr="make[1]: *** [Makefile:71: sdk-plugin-validate] Error 1\n",
        diagnostic=document("validate-compiler-diagnostic"),
    )
    result = classify(failure)
    assert (result.category, result.rule_id, result.confidence) == (
        "misconfiguration",
        "MC006",
        "deterministic",
    )
    assert result.facts == {"codes": ["TARGET_PATH_INVALID"]}
    assert result.evidence == (
        "TARGET_PATH_INVALID plugin.yaml:1: 'missing-target.yaml' is not a regular file.",
    )
    assert any("Reference an existing regular file" in action for action in result.actions)
