"""Tests for the execution-context builder."""

from __future__ import annotations

import pytest
from syntara_plugin.testing import build_execution_context


def test_defaults_build_a_valid_unique_context() -> None:
    first = build_execution_context()
    second = build_execution_context()
    assert first.invocation_id.startswith("test-invocation-")
    assert first.invocation_id != second.invocation_id
    assert first.workflow_run_id is None
    assert first.runtime_context == {}
    assert not first.cancelled


def test_overrides_are_applied() -> None:
    context = build_execution_context(
        invocation_id="stable-id",
        workflow_run_id="run-42",
        runtime_context={"environment": "test"},
    )
    assert context.invocation_id == "stable-id"
    assert context.workflow_run_id == "run-42"
    assert context.runtime_context == {"environment": "test"}


def test_cancelled_presets_the_cancellation_signal() -> None:
    assert build_execution_context(cancelled=True).cancelled
    assert not build_execution_context(cancelled=False).cancelled


def test_empty_invocation_id_is_rejected_by_the_context_contract() -> None:
    with pytest.raises(ValueError, match="invocation_id"):
        build_execution_context(invocation_id="")
