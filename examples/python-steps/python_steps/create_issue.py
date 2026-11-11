"""An example Action step: create one issue in an external tracker."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel
from syntara_plugin.runtime import ActionStep, ExecutionContext


class CreateIssueInput(BaseModel):
    """The declared input contract; mirrors steps/create-issue/manifest.yaml."""

    title: str
    description: str = ""
    request_type: Literal["bug", "feature"] = "bug"
    priority: Literal["low", "medium", "high"] = "low"
    estimate_points: int = 3
    notify_watchers: bool = True
    due_date: date | None = None
    reproduction_steps: str | None = None
    environment: Literal["development", "staging", "production"] = "development"
    business_value: str | None = None
    target_release: str | None = None
    api_token: str = ""

class CreateIssueOutput(BaseModel):
    """The declared output contract; never echoes the test API token."""

    issue_url: str


class CreateIssueStep(ActionStep[CreateIssueInput, CreateIssueOutput]):
    """Create one issue; the test API token is never echoed."""

    def run(self, inputs: CreateIssueInput, context: ExecutionContext) -> CreateIssueOutput:
        slug = inputs.title.lower().replace(" ", "-")
        return CreateIssueOutput(issue_url=f"https://issues.example.test/{slug}")
