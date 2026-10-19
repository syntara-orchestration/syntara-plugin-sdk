"""An example Action step: create one issue in an external tracker."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class CreateIssueInput(BaseModel):
    """The declared input contract; mirrors steps/create-issue/manifest.yaml."""

    title: str
    priority: Literal["low", "medium", "high"] = "low"
    api_token: str = ""


class CreateIssueOutput(BaseModel):
    """The declared output contract; never echoes the API token."""

    issue_url: str


class CreateIssueStep(ActionStep[CreateIssueInput, CreateIssueOutput]):
    """Create one issue; the api_token is accepted but never echoed."""

    def run(self, inputs: CreateIssueInput, context: ExecutionContext) -> CreateIssueOutput:
        slug = inputs.title.lower().replace(" ", "-")
        return CreateIssueOutput(issue_url=f"https://issues.example.test/{slug}")
