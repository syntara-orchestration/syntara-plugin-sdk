"""Tests for the execution-context builder."""

from __future__ import annotations

import pytest
from syntara_plugin.testing import build_execution_context


def test_defaults_build_a_valid_unique_context() -> None:
    first = build_execution_context()
    second = build_execution_context()
    assert first.invocation_id.startswith("test-invocation-")
    assert first.invocation_id != second.invocation_id
    assert not hasattr(first, "workflow_run_id")
    assert first.runtime_context == {}
    assert not first.cancelled


def test_overrides_are_applied() -> None:
    context = build_execution_context(
        invocation_id="stable-id",

        runtime_context={"execution_id": "run-42", "environment": "test"},
    )
    assert context.invocation_id == "stable-id"
    assert context.runtime_context.get("execution_id", "") == "run-42"
    assert context.runtime_context == {"environment": "test", "execution_id": "run-42"}


def test_cancelled_presets_the_cancellation_signal() -> None:
    assert build_execution_context(cancelled=True).cancelled
    assert not build_execution_context(cancelled=False).cancelled


def test_empty_invocation_id_is_rejected_by_the_context_contract() -> None:
    with pytest.raises(ValueError, match="invocation_id"):
        build_execution_context(invocation_id="")


def test_non_identity_invocation_ids_are_rejected_with_guidance() -> None:
    from syntara_plugin.testing import HarnessUsageError

    with pytest.raises(HarnessUsageError, match="Syntara identity"):
        build_execution_context(invocation_id="not a valid id")
    with pytest.raises(HarnessUsageError, match="Syntara identity"):
        build_execution_context(invocation_id="x" * 257)


def test_invocation_ids_use_the_full_abi_limit() -> None:
    context = build_execution_context(invocation_id="x" * 256)
    assert context.invocation_id == "x" * 256


def test_caller_context_has_no_domain_identity_constraints() -> None:
    context = build_execution_context(runtime_context={"execution_id": "caller chosen value"})
    assert context.runtime_context["execution_id"] == "caller chosen value"


def test_removed_workflow_run_argument_is_rejected() -> None:
    with pytest.raises(TypeError, match="workflow_run_id"):
        build_execution_context(workflow_run_id="run-1")  # type: ignore[call-arg]
