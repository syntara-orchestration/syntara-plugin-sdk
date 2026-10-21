"""Tests for entry-point parsing, loading, and step resolution."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, BaseStep, ControlStep, ExecutionContext
from syntara_plugin.testing import (
    HarnessUsageError,
    ReferenceControlStep,
    entry_point_of,
    load_step_class,
    parse_entry_point,
    resolve_step,
)


class LocalInput(BaseModel):
    value: str = "x"


class LocalOutput(BaseModel):
    result: str = "y"


class LocalTask(ControlStep[LocalInput, LocalOutput]):
    def run(self, inputs: LocalInput, context: ExecutionContext) -> LocalOutput:
        return LocalOutput(result=inputs.value)


class LocalAction(ActionStep[LocalInput, LocalOutput]):
    def run(self, inputs: LocalInput, context: ExecutionContext) -> LocalOutput:
        return LocalOutput(result=inputs.value)


class NeedsArguments(ControlStep[LocalInput, LocalOutput]):
    def __init__(self, anything: int) -> None:
        super().__init__()
        self.anything = anything

    def run(self, inputs: LocalInput, context: ExecutionContext) -> LocalOutput:
        return LocalOutput(result=inputs.value)


def test_parse_entry_point_splits_module_and_class() -> None:
    assert parse_entry_point("pkg.steps.notify:NotifyStep") == ("pkg.steps.notify", "NotifyStep")


@pytest.mark.parametrize(
    "entry_point", ["", "no-colon", ":NoModule", "pkg.mod:", "pkg.mod:9Class", "a b:C"]
)
def test_parse_entry_point_rejects_malformed_strings(entry_point: str) -> None:
    with pytest.raises(HarnessUsageError):
        parse_entry_point(entry_point)


def test_load_step_class_imports_module_level_step() -> None:
    step_class = load_step_class(f"{__name__}:LocalTask")
    assert step_class is LocalTask


def test_load_step_class_rejects_missing_module() -> None:
    with pytest.raises(HarnessUsageError, match="could not import module"):
        load_step_class("not_a_real_module_xyz:Step")


def test_load_step_class_rejects_missing_class() -> None:
    with pytest.raises(HarnessUsageError, match="has no class"):
        load_step_class(f"{__name__}:NotDefinedHere")


def test_load_step_class_rejects_non_step_attribute() -> None:
    with pytest.raises(HarnessUsageError, match="must name a BaseStep subclass"):
        load_step_class("json:dumps")


def test_entry_point_of_round_trips_through_load_step_class() -> None:
    entry_point = entry_point_of(LocalTask)
    assert entry_point == f"{__name__}:LocalTask"
    assert load_step_class(entry_point) is LocalTask


def test_resolve_step_from_class_instance_and_entry_point() -> None:
    from_class = resolve_step(LocalTask)
    assert isinstance(from_class, LocalTask)
    assert resolve_step(from_class) is from_class
    from_entry_point = resolve_step(f"{__name__}:LocalTask")
    assert isinstance(from_entry_point, LocalTask)


def test_resolve_step_rejects_non_step_objects() -> None:
    with pytest.raises(HarnessUsageError, match="module:Class"):
        resolve_step("just a string without a colon")  # type: ignore[arg-type]


def test_resolve_step_rejects_classes_needing_arguments() -> None:
    with pytest.raises(HarnessUsageError, match="constructible without arguments"):
        resolve_step(NeedsArguments)  # type: ignore[arg-type]


def test_reference_step_is_a_task_base_step() -> None:
    step: BaseStep[Any, Any] = ReferenceControlStep()
    assert isinstance(step, ControlStep)
