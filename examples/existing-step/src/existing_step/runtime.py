"""Python implementations and installed-manifest bindings for the prototype."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from syntara_plugin.runtime import (
    ActionStep,
    ControlStep,
    ExecutionContext,
    StepBindings,
    TriggerStep,
    load_plugin_bindings,
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
    """Bind every packaged custom-runner Step this plugin declares."""

    return load_plugin_bindings("existing_step")
