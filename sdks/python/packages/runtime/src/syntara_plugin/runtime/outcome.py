"""Runtime-local outcome envelope used before the common outcome story lands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ErrorMessage(BaseModel):
    """A safe, actionable error carried by the standard output wrapper."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    message: str = Field(max_length=4_096)
    retryable: bool = False


class StandardOutputWrapper(BaseModel):
    """The SDK-owned result envelope returned by the raw step executor."""

    model_config = ConfigDict(frozen=True)

    status: int = Field(ge=0)
    status_message: str = Field(default="", max_length=4_096)
    output: Mapping[str, Any] | None = None
    error: ErrorMessage | None = None

    @model_validator(mode="after")
    def _validate_outcome(self) -> StandardOutputWrapper:
        """Keep success and deliberate failure outcomes internally consistent."""

        if self.status == 0:
            if self.error is not None:
                raise ValueError("a successful outcome must not carry an error")
            if self.output is None:
                raise ValueError("a successful outcome requires an output object")
        elif self.error is None:
            raise ValueError("a failed outcome requires an error")
        return self

    @classmethod
    def success(cls, output: Mapping[str, Any]) -> StandardOutputWrapper:
        """Build the envelope for one successfully completed step."""

        return cls(status=0, status_message="Completed", output=output)

    @classmethod
    def failure(
        cls, error: ErrorMessage, *, output: Mapping[str, Any] | None = None
    ) -> StandardOutputWrapper:
        """Build the envelope for one deliberately failed step."""

        return cls(status=1, status_message=error.message, output=output, error=error)
