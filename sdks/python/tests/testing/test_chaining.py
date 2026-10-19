"""Tests for sequential chaining and seam compatibility checks."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ExecutionContext,
    StepBindings,
    StepDescriptor,
    TaskStep,
)
from syntara_plugin.testing import (
    chain_steps,
    check_seam_compatible,
    identity_seam,
)
from syntara_plugin.testing.payloads import FailingReferenceStep


class UpperInput(BaseModel):
    text: str


class UpperOutput(BaseModel):
    upper: str


class UpperTask(TaskStep[UpperInput, UpperOutput]):
    def run(self, inputs: UpperInput, context: ExecutionContext) -> UpperOutput:
        return UpperOutput(upper=inputs.text.upper())


class CountVowelsInput(BaseModel):
    upper: str


class CountVowelsOutput(BaseModel):
    vowel_count: int


class CountVowelsTask(TaskStep[CountVowelsInput, CountVowelsOutput]):
    def run(self, inputs: CountVowelsInput, context: ExecutionContext) -> CountVowelsOutput:
        return CountVowelsOutput(
            vowel_count=sum(character in "AEIOU" for character in inputs.upper)
        )


class StrictInput(BaseModel):
    upper: str
    mandatory_extra: str


class StrictTask(TaskStep[StrictInput, CountVowelsOutput]):
    def run(self, inputs: StrictInput, context: ExecutionContext) -> CountVowelsOutput:
        return CountVowelsOutput(vowel_count=0)


class CaptureIdInput(BaseModel):
    text: str


class CaptureIdOutput(BaseModel):
    invocation_id: str
    workflow_run_id: str


class CaptureIdTask(TaskStep[CaptureIdInput, CaptureIdOutput]):
    def run(self, inputs: CaptureIdInput, context: ExecutionContext) -> CaptureIdOutput:
        return CaptureIdOutput(
            invocation_id=context.invocation_id,
            workflow_run_id=context.workflow_run_id or "",
        )


def _bind(step: Any, identity: str) -> Any:
    descriptor = StepDescriptor.for_step(identity, step())
    bindings = StepBindings()
    bindings.bind(descriptor, step())
    return bindings.binding(identity)


def test_chain_executes_steps_sequentially_through_a_seam() -> None:
    outcome = chain_steps(
        [_bind(UpperTask, "chain.upper"), _bind(CountVowelsTask, "chain.count")],
        {"text": "hello"},
        seams=[identity_seam],
    )
    assert outcome.ok
    assert outcome.output == {"vowel_count": 2}
    assert outcome.failed_link is None
    assert [link.ok for link in outcome.links] == [True, True]


def test_chain_default_seam_passes_output_through() -> None:
    outcome = chain_steps(
        [_bind(UpperTask, "chain.upper"), _bind(CountVowelsTask, "chain.count")],
        {"text": "queue"},
    )
    assert outcome.ok
    assert outcome.output == {"vowel_count": 4}


def test_chain_gives_each_link_its_own_invocation_id() -> None:
    outcome = chain_steps(
        [_bind(CaptureIdTask, "chain.id-one"), _bind(CaptureIdTask, "chain.id-two")],
        {"text": "x"},
        seams=[lambda output: {"text": str(output["invocation_id"])}],
    )
    assert outcome.ok
    first_id = outcome.links[0].output["invocation_id"]
    second_id = outcome.links[1].output["invocation_id"]
    assert first_id != second_id
    assert outcome.links[0].output["workflow_run_id"] == (
        outcome.links[1].output["workflow_run_id"]
    )


def test_chain_fails_fast_naming_the_broken_link() -> None:
    outcome = chain_steps(
        [
            _bind(UpperTask, "chain.upper"),
            _bind(FailingReferenceStep, "chain.failing"),
            _bind(CountVowelsTask, "chain.count"),
        ],
        {"text": "hello"},
        seams=[identity_seam, identity_seam],
    )
    assert not outcome.ok
    assert outcome.failed_link == 1
    assert outcome.failed_step_identity == "chain.failing"
    assert outcome.output is None
    assert len(outcome.links) == 2


def test_chain_rejects_empty_bindings() -> None:
    with pytest.raises(ValueError, match="at least one step binding"):
        chain_steps([], {"text": "x"})


def test_chain_rejects_wrong_seam_counts() -> None:
    with pytest.raises(ValueError, match="seams"):
        chain_steps(
            [_bind(UpperTask, "chain.upper"), _bind(CountVowelsTask, "chain.count")],
            {"text": "hello"},
            seams=[identity_seam, identity_seam],
        )


def test_check_seam_compatible_passes_for_matching_schemas() -> None:
    source = _bind(UpperTask, "chain.upper")
    target = _bind(CountVowelsTask, "chain.count")
    report = check_seam_compatible(source, target)
    assert report.passed(), report.issues


def test_check_seam_compatible_detects_missing_required_fields() -> None:
    source = _bind(UpperTask, "chain.upper")
    target = _bind(StrictTask, "chain.strict")
    report = check_seam_compatible(source, target)
    assert not report.passed()
    assert report.errors
