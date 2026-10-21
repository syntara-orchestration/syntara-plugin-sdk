"""Tests for sequential chaining and seam compatibility checks."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ControlStep,
    ExecutionContext,
    StepBindings,
)
from syntara_plugin.testing import (
    chain_steps,
    check_seam_compatible,
    identity_seam,
)
from syntara_plugin.testing.bindings_factory import installed_binding


class UpperInput(BaseModel):
    text: str


class UpperOutput(BaseModel):
    upper: str


class UpperTask(ControlStep[UpperInput, UpperOutput]):
    def run(self, inputs: UpperInput, context: ExecutionContext) -> UpperOutput:
        return UpperOutput(upper=inputs.text.upper())


class CountVowelsInput(BaseModel):
    upper: str


class CountVowelsOutput(BaseModel):
    vowel_count: int


class CountVowelsTask(ControlStep[CountVowelsInput, CountVowelsOutput]):
    def run(self, inputs: CountVowelsInput, context: ExecutionContext) -> CountVowelsOutput:
        return CountVowelsOutput(
            vowel_count=sum(character in "AEIOU" for character in inputs.upper)
        )


class StrictInput(BaseModel):
    upper: str
    mandatory_extra: str


class StrictTask(ControlStep[StrictInput, CountVowelsOutput]):
    def run(self, inputs: StrictInput, context: ExecutionContext) -> CountVowelsOutput:
        return CountVowelsOutput(vowel_count=0)


class CaptureIdInput(BaseModel):
    text: str


class CaptureIdOutput(BaseModel):
    invocation_id: str
    workflow_run_id: str


class CaptureIdTask(ControlStep[CaptureIdInput, CaptureIdOutput]):
    def run(self, inputs: CaptureIdInput, context: ExecutionContext) -> CaptureIdOutput:
        return CaptureIdOutput(
            invocation_id=context.invocation_id,
            workflow_run_id=context.workflow_run_id or "",
        )


def _bind(step: Any, identity: str) -> Any:
    binding = installed_binding(step(), identity=identity)
    bindings = StepBindings()
    bindings.bind(binding.descriptor, binding.step)
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


class _FailingVowelsStep(ControlStep[CountVowelsInput, CountVowelsOutput]):
    """Fails from the business hook."""

    def run(self, inputs: CountVowelsInput, context: ExecutionContext) -> CountVowelsOutput:
        raise RuntimeError("middle link failed")


def test_chain_fails_fast_naming_the_broken_link() -> None:
    bindings = [
        _bind(UpperTask, "chain.upper"),
        _bind(_FailingVowelsStep, "chain.failing"),
        _bind(CountVowelsTask, "chain.count"),
    ]
    outcome = chain_steps(
        bindings,
        {"text": "hello"},
        seams=[identity_seam, identity_seam],
    )
    assert not outcome.ok
    assert outcome.failed_link == 1
    assert outcome.failed_step_identity == "chain.failing"
    assert outcome.output is None
    assert len(outcome.links) == 2
    assert not outcome.seam_issues


def test_chain_rejects_empty_bindings() -> None:
    with pytest.raises(ValueError, match="at least one step binding"):
        chain_steps([], {"text": "x"})


def test_chain_rejects_wrong_seam_counts() -> None:
    bindings = [
        _bind(UpperTask, "chain.upper"),
        _bind(CountVowelsTask, "chain.count"),
    ]
    with pytest.raises(ValueError, match="seams"):
        chain_steps(bindings, {"text": "hello"}, seams=[identity_seam, identity_seam])


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


class _IntInput(BaseModel):
    value: int


class _IntOutput(BaseModel):
    value: int


class _IntSourceStep(ControlStep[CaptureIdInput, _IntOutput]):
    """Emits an integer output for seam validation."""

    def run(self, inputs: CaptureIdInput, context: ExecutionContext) -> _IntOutput:
        return _IntOutput(value=1)


class _IntTargetStep(ControlStep[_IntInput, _IntOutput]):
    """Consumes an integer input."""

    def run(self, inputs: _IntInput, context: ExecutionContext) -> _IntOutput:
        return _IntOutput(value=inputs.value)


def test_seam_output_validation_names_the_seam_and_blocks_the_target() -> None:
    """A seam returning schema-invalid values fails before the target runs."""

    bindings = StepBindings()
    for identity, step in (
        ("chain.json.source", _IntSourceStep()),
        ("chain.json.target", _IntTargetStep()),
    ):
        binding = installed_binding(step, identity=identity)
        bindings.bind(binding.descriptor, binding.step)
    source = bindings.binding("chain.json.source")
    target = bindings.binding("chain.json.target")

    # The declared contracts fit, so the static check passes...
    assert check_seam_compatible(source, target).passed()
    # ...but a seam returning the wrong type is caught at execution time,
    # named as a seam failure, and never reaches the target's hook.
    outcome = chain_steps(
        [source, target],
        {"text": "x"},
        context=None,
        seams=[lambda output: {"value": "not-an-integer"}],
    )
    assert not outcome.ok
    assert outcome.failed_link == 1
    assert outcome.seam_issues
    assert any("type" in issue for issue in outcome.seam_issues)
    # The target link is never appended: the seam failure precedes it.
    assert len(outcome.links) == 1
    assert outcome.links[-1].run_calls == 1
