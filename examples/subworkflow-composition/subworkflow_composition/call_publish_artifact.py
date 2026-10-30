"""The parent-side subworkflow call.

The contract-true parent declares ``runtime.kind: platform-runner`` with
``implementation: syntara.workflow.invoke@1.0.0``; see
``contracts/fixtures/subworkflow-call.valid.json``. The platform owns that
runner, so it cannot execute locally. This custom-runner Control stands in for
it: it declares the same input and output contracts and surfaces the child
workflow's result, which is what makes the composition seams testable offline.
"""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ControlStep, ExecutionContext

from .publish_artifact import PublishArtifactInput, PublishArtifactStep
from .publish_artifact_requested import (
    PublishArtifactRequestedInput,
    PublishArtifactRequestedStep,
)


class CallPublishArtifactInput(BaseModel):
    """The parameters the parent workflow passes to the child workflow."""

    artifact: str
    channel: str


class CallPublishArtifactOutput(BaseModel):
    """The result the child workflow surfaces to its parent."""

    releaseId: str
    publishedAt: str


class CallPublishArtifactStep(
    ControlStep[CallPublishArtifactInput, CallPublishArtifactOutput]
):
    """Invoke the child workflow and return its declared result.

    The stand-in runs the child's trigger and terminal step in process. It
    deliberately does not resolve a published workflow version, evaluate a
    Used-By dependency guard, or start a durable child run: those belong to
    the control plane and are what the platform runner would provide.
    """

    def run(
        self, inputs: CallPublishArtifactInput, context: ExecutionContext
    ) -> CallPublishArtifactOutput:
        event = PublishArtifactRequestedStep().run(
            PublishArtifactRequestedInput(
                artifact=inputs.artifact, channel=inputs.channel
            ),
            context,
        )
        result = PublishArtifactStep().run(
            PublishArtifactInput(**event.model_dump()), context
        )
        return CallPublishArtifactOutput(
            releaseId=result.releaseId, publishedAt=result.publishedAt
        )
