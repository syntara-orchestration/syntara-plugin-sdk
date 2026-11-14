"""Classifier tests for the contract rules (CT001-CT007).

These pin the SDK/step-types boundary: the dependency-resolution
fall-through (CT002), the runtime-boundary failure codes a run document
reports (CT007), gRPC protocol failures surfaced as stderr text (CT004),
and the order the contract rules win and lose against traceback
provenance and against each other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from classifier import INTENTIONAL, Failure, classify

MAKE_RUN_ERROR = "make[1]: *** [Makefile:75: sdk-plugin-run] Error 1\n"
STEP_TYPES_DIR = Path("/work/step-types")


def run_failure(diagnostic: Any = None, **fields: Any) -> Failure:
    """A failed run stage, shaped the way the driver captures it."""
    base: dict[str, Any] = {
        "stage": "run",
        "plugin": "http-request",
        "exit_code": 2,
        "stdout": "false\n",
        "stderr": MAKE_RUN_ERROR,
        "diagnostic": diagnostic,
        "step_types_dir": STEP_TYPES_DIR,
    }
    base.update(fields)
    return Failure(**base)


def run_document(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """A failed ``run --json`` document in the shape the CLI really emits."""
    document: dict[str, Any] = {
        "credentials": [],
        "error": {"code": code, "message": message, "providerCode": "", "retryable": False},
        "errorDetail": None,
        "image": None,
        "input": {"method": "GET", "url": "https://httpbin.org/get"},
        "inputSource": "example.json",
        "invocationId": "run-00000000000000000000000000000000",
        "mode": "source",
        "objectReference": None,
        "ok": False,
        "outcome": None,
        "output": None,
        "status": 1,
        "statusMessage": message,
        "target": "syntara.http-request.http-request",
        "workflowRunId": "run-00000000000000000000000000000000-run",
    }
    document.update(extra)
    return document


def test_environment_failure_without_resolution_signature_is_not_dependency_resolution() -> None:
    # uv failed to prepare the candidate wheels for a reason that is not
    # dependency resolution (the text even carries the "depends on" detail
    # word); _resolution must fall through instead of claiming CT002.
    stderr = (
        "make: Entering directory '/work/step-types'\n"
        "  × Failed to prepare distributions\n"
        "  ├─▶ Because syntara-plugin-sdk==0.1.0a5 depends on hatchling==1.24, "
        "which has no wheel for this platform, its sdist build failed\n"
        "  ╰─▶ Backend `hatchling.build` exited with code 1: README missing\n"
        "make: *** [Makefile:38: sdk-plugin-environment] Error 1\n"
        "make: Leaving directory '/work/step-types'\n"
    )
    result = classify(
        Failure(stage="environment", plugin="http-request", exit_code=1, stderr=stderr)
    )
    assert (result.category, result.rule_id, result.confidence) == (
        "unknown",
        "UN000",
        "unclassified",
    )


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("ABI_VIOLATION", "step runtime sent an event outside the declared runtime ABI"),
        ("CREDENTIAL_SCAN_LIMIT", "credential scan exceeded the limit while reading step output"),
    ],
)
def test_run_document_boundary_code_is_runtime_boundary_contract(code: str, message: str) -> None:
    result = classify(run_failure(run_document(code, message)))
    assert (result.category, result.rule_id, result.confidence) == (
        "contract",
        "CT007",
        "deterministic",
    )
    assert result.rule_name == "runtime-boundary"
    assert result.evidence == (f"error.code {code}: {message}",)
    assert result.facts == {"code": code, "message": message}
    assert result.actions == (INTENTIONAL,)


@pytest.mark.parametrize(
    "rpc_failure",
    [
        # The runner caught the RPC error and printed one line.
        "error: cannot reach the step runtime: StatusCode.UNAVAILABLE\n",
        # The canonical grpc error block, as it lands in captured stderr.
        (
            "grpc._channel._InactiveRpcError: <_InactiveRpcError of RPC that terminated with:\n"
            "\tstatus = StatusCode.UNAVAILABLE\n"
            '\tdetails = "Socket closed"\n'
            ">\n"
        ),
    ],
)
def test_grpc_failure_without_a_run_document_is_inferred_contract(rpc_failure: str) -> None:
    # The runner died before writing a run document, so only its stderr and
    # the make wrapper's verdict were captured.
    result = classify(run_failure(stdout="", exit_code=1, stderr=rpc_failure + MAKE_RUN_ERROR))
    assert (result.category, result.rule_id, result.confidence) == (
        "contract",
        "CT004",
        "inferred",
    )
    assert result.rule_name == "grpc-protocol"
    assert any("UNAVAILABLE" in line or "_InactiveRpcError" in line for line in result.evidence)
    assert any("traceback analysis below" in action for action in result.actions)


def test_grpc_flavoured_sdk_crash_is_still_an_sdk_regression() -> None:
    # A traceback proves the side even when the exception is a gRPC error:
    # provenance must win over the CT004 text signature.
    crash = run_document(
        "OUTCOME_UNKNOWN",
        "The step did not complete.",
        errorDetail=(
            "Traceback (most recent call last):\n"
            '  File "/work/step-types/http-request/src/syntara_node_http_request/__init__.py", '
            "line 40, in run\n"
            "    channel = context.runtime_channel()\n"
            '  File "/work/step-types/.venv/lib/python3.12/site-packages/syntara_plugin/runtime/'
            'channel.py", line 9, in call\n'
            "    raise grpc._channel._InactiveRpcError(state)\n"
            "grpc._channel._InactiveRpcError: <_InactiveRpcError of RPC that terminated with:\n"
            "\tstatus = StatusCode.UNAVAILABLE\n"
            '\tdetails = "Socket closed"\n'
            ">\n"
        ),
    )
    result = classify(run_failure(crash))
    assert (result.category, result.rule_id) == ("sdk-regression", "SR001")
    assert result.evidence[0] == (
        "/work/step-types/.venv/lib/python3.12/site-packages/syntara_plugin/runtime/"
        "channel.py:9 in call"
    )
    assert result.facts["code"] == "OUTCOME_UNKNOWN"


def test_run_document_boundary_code_beats_the_grpc_signature() -> None:
    # The run document's error.code is deterministic evidence; the gRPC
    # status quoted in its message must not demote the failure to CT004.
    document = run_document(
        "ABI_VIOLATION", "step runtime aborted the RPC: StatusCode.UNAVAILABLE"
    )
    result = classify(run_failure(document))
    assert (result.category, result.rule_id, result.confidence) == (
        "contract",
        "CT007",
        "deterministic",
    )


def test_runtime_abi_signature_beats_the_grpc_signature() -> None:
    # The deterministic ABI rejection must win over the inferred gRPC
    # signal printed right after it.
    stderr = (
        "RuntimeError: The plugin container does not support the requested runtime ABI.\n"
        "StatusCode.UNAVAILABLE: the RPC to the step runtime terminated after the rejection\n"
        + MAKE_RUN_ERROR
    )
    result = classify(run_failure(stdout="", exit_code=1, stderr=stderr))
    assert (result.category, result.rule_id, result.confidence) == (
        "contract",
        "CT001",
        "deterministic",
    )


def test_step_output_quoting_a_grpc_status_is_not_a_contract_failure() -> None:
    # The step ran and its output quotes a gRPC status it observed. Output is
    # step-produced data, like the echoed input, and must not trigger a rule.
    document = {
        "ok": True,
        "output": {"status_code": 200, "stdout": "last error was StatusCode.UNAVAILABLE, retrying"},
    }
    result = classify(run_failure(document))
    assert (result.category, result.rule_id, result.confidence) == (
        "implementation",
        "IM001",
        "deterministic",
    )


def test_exception_line_matches_underscore_prefixed_exception_names() -> None:
    # gRPC exception classes start with an underscore after their module path.
    from classifier import _exception_line

    line = "grpc._channel._InactiveRpcError: <_InactiveRpcError of RPC that terminated>"
    assert _exception_line(line + "\n") == line
    assert _exception_line("httpx.ConnectError: refused\n") == "httpx.ConnectError: refused"


def test_runtime_input_rejection_code_is_input_validation_contract() -> None:
    # The runtime validates the step's inputs before the step runs, so a
    # code-only INVALID_INPUT report is the SDK rejecting step-types' example
    # input, not the step failing. Real shape: no error.kind, no errorDetail.
    result = classify(run_failure(run_document("INVALID_INPUT", "input does not satisfy the declared schema")))
    assert (result.category, result.rule_id, result.confidence) == (
        "contract",
        "CT006",
        "deterministic",
    )
    assert result.facts == {"code": "INVALID_INPUT", "message": "input does not satisfy the declared schema"}


def test_outcome_unknown_without_a_traceback_is_not_attributed_to_the_step() -> None:
    # The SDK's catch-all reports OUTCOME_UNKNOWN with errorDetail null; without
    # a traceback nothing establishes an origin, so the analysis stays unknown
    # instead of claiming the step failed.
    result = classify(run_failure(run_document("OUTCOME_UNKNOWN", "The step did not complete.")))
    assert (result.category, result.rule_id, result.confidence) == (
        "unknown",
        "UN000",
        "unclassified",
    )
    assert result.facts == {"code": "OUTCOME_UNKNOWN"}
    assert result.evidence == ("error.code OUTCOME_UNKNOWN: The step did not complete.",)


def test_unmapped_runtime_code_stays_unknown_and_points_at_the_mapping() -> None:
    result = classify(run_failure(run_document("SOME_NEW_CODE", "a code the SDK added")))
    assert (result.category, result.rule_id) == ("unknown", "UN000")
    assert any("run-code sets" in action for action in result.actions)


def test_code_only_cancellation_is_not_attributed_to_the_step() -> None:
    # The SDK reports CANCELLED both for step-reported cancellations and for
    # cancelled transport RPCs; without a traceback neither is established.
    result = classify(run_failure(run_document("CANCELLED", "HTTP request was cancelled.")))
    assert (result.category, result.rule_id, result.confidence) == (
        "unknown",
        "UN000",
        "unclassified",
    )
    assert result.facts == {"code": "CANCELLED"}
