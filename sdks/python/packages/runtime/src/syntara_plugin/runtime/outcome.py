"""Runtime-local outcome envelope used before the common outcome story lands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field


class ErrorMessage(BaseModel):
    """A safe, actionable error carried by the standard output wrapper."""

    code: str
    message: str = Field(max_length=4_096)
    retryable: bool = False


class StandardOutputWrapper(BaseModel):
    """The SDK-owned result envelope returned by the raw step executor."""

    status: int = Field(ge=0)
    output: Mapping[str, Any] | None = None
    error: ErrorMessage | None = None
