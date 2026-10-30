"""The child-side subworkflow trigger: the entry point of the child workflow."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ExecutionContext, TriggerStep


class PublishArtifactRequestedInput(BaseModel):
    """The parameters the invoking parent workflow supplies."""

    artifact: str
    channel: str


class PublishArtifactRequestedOutput(BaseModel):
    """The event handed to the child workflow's first step."""

    artifact: str
    channel: str
    parentRunId: str


class PublishArtifactRequestedStep(
    TriggerStep[PublishArtifactRequestedInput, PublishArtifactRequestedOutput]
):
    """Emit the invocation event that starts the child workflow.

    This is a Python-bound trigger implementation, distinct from the
    platform-managed ``subworkflow.v1`` driver declared in the manifest:
    direct SDK execution exercises this class, while dispatching a child run
    from a parent workflow is owned by the platform.
    """

    def run(
        self, inputs: PublishArtifactRequestedInput, context: ExecutionContext
    ) -> PublishArtifactRequestedOutput:
        return PublishArtifactRequestedOutput(
            artifact=inputs.artifact,
            channel=inputs.channel,
            parentRunId=context.workflow_run_id or "no-parent-run",
        )
