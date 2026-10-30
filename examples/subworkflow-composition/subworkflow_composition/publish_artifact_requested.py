"""The child-side subworkflow trigger: the entry point of the child workflow."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ExecutionContext, TriggerStep


class PublishArtifactRequestedInput(BaseModel):
    """The parameters the invoking parent workflow supplies."""

    artifact: str
    channel: str


class PublishArtifactRequestedOutput(BaseModel):
    """The event handed to the child workflow's first step.

    Deliberately carries no parent-run identifier: ``ExecutionContext`` has no
    parent-run field, and the run the trigger executes in is the child's.
    Parent/child run correlation is a runtime-context gap (AAP-94805).
    """

    artifact: str
    channel: str


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
        )
