"""Sample payload cases and reference steps for the seven contract categories."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel
from syntara_plugin.runtime import (
    ExecutionContext,
    FailureCode,
    StepBinding,
    StepDescriptor,
    StepTaxonomy,
    TaskStep,
)
from syntara_plugin.runtime.container import ContainerInvocationError

#: Fixed sentinel for the credential-reference payload case. Use
#: :func:`syntara_plugin.testing.secret_sentinel` for dynamic, unique values.
FIXED_SECRET_SENTINEL = "syntara-test-secret-fixed"


class Expectation(StrEnum):
    """The outcome a payload case must produce through the SDK executor."""

    VALID = "valid"
    INVALID_INPUT = "invalid_input"
    DELIBERATE_FAILURE = "deliberate_failure"
    INVALID_OUTPUT = "invalid_output"


@dataclass(frozen=True)
class PayloadCase:
    """One named input payload and the outcome it must produce."""

    name: str
    inputs: Mapping[str, Any]
    expectation: Expectation


class ReferenceInput(BaseModel):
    """A representative typed input: required, enum, numeric, list, secret-like fields."""

    title: str
    priority: Literal["low", "medium", "high"] = "low"
    count: int = 1
    tags: list[str] = []
    api_token: str = ""


class ReferenceOutput(BaseModel):
    """A representative typed output."""

    summary: str
    priority: str


class ReferenceTaskStep(TaskStep[ReferenceInput, ReferenceOutput]):
    """A representative Task implementation used by harness tests and examples."""

    def run(self, inputs: ReferenceInput, context: ExecutionContext) -> ReferenceOutput:
        return ReferenceOutput(summary=f"processed {inputs.title}", priority=inputs.priority)


class FailingReferenceStep(TaskStep[ReferenceInput, ReferenceOutput]):
    """A Task that deliberately fails with a safe, retryable error."""

    def run(self, inputs: ReferenceInput, context: ExecutionContext) -> ReferenceOutput:
        raise ContainerInvocationError(
            FailureCode.UPSTREAM_TRANSIENT,
            "The upstream service rejected the request.",
            retryable=True,
        )


_INVALID_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "maxLength": 3},
        "priority": {"type": "string"},
    },
    "required": ["summary", "priority"],
    "additionalProperties": False,
}


def schema_violating_binding(
    identity: str = "syntara.testing.schema-violating-task",
) -> StepBinding:
    """Bind the reference Task with an output schema its result violates.

    The declared output schema is deliberately stricter than the
    implementation's model, so a successful ``run()`` result fails the
    executor's execution-time output-schema validation. This binding is the
    harness's stand-in for a manifest-declared output contract that drifted
    against its implementation; it is intentionally not drift-verified.
    """

    step = ReferenceTaskStep()
    descriptor = StepDescriptor(
        step_identity=identity,
        taxonomy=StepTaxonomy.TASK,
        input_schema=ReferenceInput.model_json_schema(),
        output_schema=_INVALID_OUTPUT_SCHEMA,
    )
    return StepBinding(descriptor, step)


VALID_INPUT_CASES: tuple[PayloadCase, ...] = (
    PayloadCase("valid_minimal", {"title": "Deploy failed"}, Expectation.VALID),
    PayloadCase(
        "valid_full",
        {"title": "Deploy failed", "priority": "high", "count": 2, "tags": ["production"]},
        Expectation.VALID,
    ),
    PayloadCase(
        "valid_credential_reference",
        {"title": "Deploy failed", "api_token": FIXED_SECRET_SENTINEL},
        Expectation.VALID,
    ),
)

INVALID_INPUT_CASES: tuple[PayloadCase, ...] = (
    PayloadCase("missing_required_title", {"priority": "high"}, Expectation.INVALID_INPUT),
    PayloadCase(
        "wrong_type_count", {"title": "Deploy failed", "count": "two"}, Expectation.INVALID_INPUT
    ),
    PayloadCase(
        "invalid_enum_priority",
        {"title": "Deploy failed", "priority": "urgent"},
        Expectation.INVALID_INPUT,
    ),
)

DELIBERATE_FAILURE_CASES: tuple[PayloadCase, ...] = (
    PayloadCase("deliberate_failure", {"title": "Deploy failed"}, Expectation.DELIBERATE_FAILURE),
)

INVALID_OUTPUT_CASES: tuple[PayloadCase, ...] = (
    PayloadCase("schema_invalid_output", {"title": "Deploy failed"}, Expectation.INVALID_OUTPUT),
)

#: The seven ticket categories: valid, missing-required, wrong-type, invalid-enum,
#: schema-invalid output, deliberate failure, and sensitive credential references.
#: Pair the cases with the matching reference step: input cases with
#: :class:`ReferenceTaskStep`, the deliberate-failure case with
#: :class:`FailingReferenceStep`, and the schema-invalid-output case with
#: :func:`schema_violating_binding`.
PAYLOAD_CASES: tuple[PayloadCase, ...] = (
    VALID_INPUT_CASES + INVALID_INPUT_CASES + DELIBERATE_FAILURE_CASES + INVALID_OUTPUT_CASES
)
