"""Python implementations and installed-manifest bindings for the prototype."""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from importlib.resources import files
from typing import Any, cast

from pydantic import BaseModel, ConfigDict
from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    ControlStep,
    ExecutionContext,
    InstalledStepManifest,
    StepBindings,
    StepDescriptor,
    TriggerStep,
)


class CreateTicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str


class CreateTicketOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str


class CreateTicketStep(ActionStep[CreateTicketInput, CreateTicketOutput]):
    def run(self, inputs: CreateTicketInput, context: ExecutionContext) -> CreateTicketOutput:
        return CreateTicketOutput(ticket_id=f"{context.invocation_id}:{inputs.title}")


class NormalizeRecordsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[str]


class NormalizeRecordsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    normalized: list[str]


class NormalizeRecordsStep(ControlStep[NormalizeRecordsInput, NormalizeRecordsOutput]):
    def run(
        self, inputs: NormalizeRecordsInput, context: ExecutionContext
    ) -> NormalizeRecordsOutput:
        return NormalizeRecordsOutput(
            count=len(inputs.records),
            normalized=[record.casefold() for record in inputs.records],
        )


class RecordOpenedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str


class RecordOpenedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    repository: str


class RecordOpenedStep(TriggerStep[RecordOpenedInput, RecordOpenedOutput]):
    def run(self, inputs: RecordOpenedInput, context: ExecutionContext) -> RecordOpenedOutput:
        workflow_run_id = context.workflow_run_id or "no-workflow"
        return RecordOpenedOutput(
            event_id=f"{workflow_run_id}:{inputs.repository}",
            repository=inputs.repository,
        )


def create_bindings() -> StepBindings:
    """Load every packaged manifest and bind its declared Python entry point."""

    root = _read_manifest("plugin.yaml")
    metadata = _mapping(root.get("metadata"), "plugin.metadata")
    specification = _mapping(root.get("spec"), "plugin.spec")
    namespace = _identifier(metadata.get("namespace"), "plugin.metadata.namespace")
    plugin_name = _identifier(metadata.get("name"), "plugin.metadata.name")
    target_paths = specification.get("targets")
    if not isinstance(target_paths, list) or not target_paths:
        raise RuntimeError("plugin.spec.targets must be a non-empty list")

    bindings = StepBindings()
    for target_path in target_paths:
        if not isinstance(target_path, str):
            raise RuntimeError("plugin.spec.targets entries must be strings")
        document = _read_manifest(target_path)
        target_metadata = _mapping(document.get("metadata"), f"{target_path}.metadata")
        target_specification = _mapping(document.get("spec"), f"{target_path}.spec")
        target_runtime = _mapping(
            target_specification.get("runtime"), f"{target_path}.spec.runtime"
        )
        entry_point = target_runtime.get("entryPoint")
        if not isinstance(entry_point, str):
            raise RuntimeError(f"{target_path}.spec.runtime.entryPoint must be a string")
        step = _load_step(entry_point)()
        identity = f"{namespace}.{plugin_name}.{_identifier(target_metadata.get('name'), f'{target_path}.metadata.name')}"
        installed_step = InstalledStepManifest.from_manifest(identity, document, {})
        bindings.bind(
            StepDescriptor.for_installed_step(step, installed_step),
            step,
        )
    return bindings


def _read_manifest(relative_path: str) -> dict[str, Any]:
    resource = files("existing_step").joinpath("manifests", *relative_path.split("/"))
    try:
        document = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not read packaged manifest {relative_path!r}") from error
    return _mapping(document, relative_path)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be an object")
    return dict(value)


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} must be a non-empty string")
    return value


def _load_step(entry_point: str) -> type[BaseStep[Any, Any]]:
    module_name, separator, class_name = entry_point.partition(":")
    if not module_name or not separator or not class_name:
        raise RuntimeError(f"invalid step entry point {entry_point!r}")
    try:
        step_class = getattr(importlib.import_module(module_name), class_name, None)
    except ImportError as error:
        raise RuntimeError(f"could not import step entry point {entry_point!r}") from error
    if not isinstance(step_class, type) or not issubclass(step_class, BaseStep):
        raise RuntimeError(f"step entry point {entry_point!r} must name a BaseStep subclass")
    return cast(type[BaseStep[Any, Any]], step_class)
