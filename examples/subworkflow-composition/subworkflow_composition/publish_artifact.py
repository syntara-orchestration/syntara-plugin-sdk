"""The child workflow's terminal step: it produces the result the parent consumes."""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext

# A real step would record the publication time. The example keeps the value
# fixed so both execution paths produce byte-identical results.
PUBLISHED_AT = "2026-01-01T00:00:00Z"


class PublishArtifactInput(BaseModel):
    """The event payload emitted by the subworkflow trigger."""

    artifact: str
    channel: str


class PublishArtifactOutput(BaseModel):
    """The child workflow's declared result; mirrors the parent's output schema."""

    releaseId: str
    publishedAt: str


class PublishArtifactStep(ActionStep[PublishArtifactInput, PublishArtifactOutput]):
    """Publish the artifact and return the result the parent workflow reads."""

    def run(
        self, inputs: PublishArtifactInput, context: ExecutionContext
    ) -> PublishArtifactOutput:
        return PublishArtifactOutput(
            releaseId=f"{inputs.channel}:{inputs.artifact}",
            publishedAt=PUBLISHED_AT,
        )
