"""An example Control-category step type: compose a remediation plan.

Control step types implement composition inside ``run()``: this example composes
normalization and planning into one opaque operation, which is exactly the
boundary the authoring guide discusses (combine operations inside one step type or
split them across Control step types).
"""

from __future__ import annotations

from pydantic import BaseModel
from syntara_plugin.runtime import ControlStep, ExecutionContext


class ComposeRemediationInput(BaseModel):
    records: list[str]


class ComposeRemediationOutput(BaseModel):
    plan: list[str]
    total: int


class ComposeRemediationStep(
    ControlStep[ComposeRemediationInput, ComposeRemediationOutput]
):
    """Normalize the records, then build one remediation action per record."""

    def run(
        self, inputs: ComposeRemediationInput, context: ExecutionContext
    ) -> ComposeRemediationOutput:
        normalized = [record.strip().lower() for record in inputs.records]
        plan = [f"remediate:{record}" for record in normalized]
        return ComposeRemediationOutput(plan=plan, total=len(plan))
