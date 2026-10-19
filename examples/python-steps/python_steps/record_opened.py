"""An example Python-bound Trigger step for opened records."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ExecutionContext, TriggerStep


class RecordOpenedInput(BaseModel):
    """The event payload consumed by the Python-bound trigger."""

    repository: str


class RecordOpenedOutput(BaseModel):
    """The event emitted by the trigger; mirrors the manifest output schema."""

    issue_url: str
    repository: str


class RecordOpenedStep(TriggerStep[RecordOpenedInput, RecordOpenedOutput]):
    """Emit one opened-record event.

    This is a Python-bound trigger implementation, distinct from the
    platform-managed ``webhook.v1`` driver declared in the manifest: direct SDK
    execution exercises this class, while platform event delivery is owned by
    the platform.
    """

    def run(
        self, inputs: RecordOpenedInput, context: ExecutionContext
    ) -> RecordOpenedOutput:
        return RecordOpenedOutput(
            issue_url=f"https://issues.example.test/{inputs.repository}/1",
            repository=inputs.repository,
        )
