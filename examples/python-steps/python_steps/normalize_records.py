"""An example Task step: normalize record names."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ExecutionContext, TaskStep


class NormalizeRecordsInput(BaseModel):
    records: list[str]


class NormalizeRecordsOutput(BaseModel):
    normalized: list[str]
    count: int


class NormalizeRecordsTask(TaskStep[NormalizeRecordsInput, NormalizeRecordsOutput]):
    """Lowercase and trim every record name."""

    def run(
        self, inputs: NormalizeRecordsInput, context: ExecutionContext
    ) -> NormalizeRecordsOutput:
        normalized = [record.strip().lower() for record in inputs.records]
        return NormalizeRecordsOutput(normalized=normalized, count=len(normalized))
