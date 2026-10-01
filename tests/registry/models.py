"""SQLModel records for the step registry."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, Index, MetaData, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class StepCategory(StrEnum):
    """Supported step palette categories."""

    ACTION = "action"
    TASK = "task"
    WORKFLOW = "workflow"
    TRIGGER = "trigger"


class RegistryBase(SQLModel):
    """Base model with isolated metadata for the registry tables."""

    metadata = MetaData()


class StepType(RegistryBase, table=True):
    """An isolated legacy storage model, not an SDK persistence contract."""

    __tablename__ = "step_types"
    __table_args__ = (
        # This test-only legacy key predates plugin-scoped canonical identity.
        UniqueConstraint(
            "namespace", "name", "version", name="uq_step_types_namespace_name_version"
        ),
        Index(
            "ix_step_types_canvas_lookup",
            "enabled",
            "category",
            "namespace",
            "name",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    namespace: str = Field(index=True, max_length=64)
    name: str = Field(index=True, max_length=64)
    display_name: str = Field(max_length=128)
    version: str = Field(default="1.0.0", max_length=64)
    category: StepCategory = Field(
        sa_column=Column(
            SAEnum(
                StepCategory,
                native_enum=False,
                create_constraint=True,
                values_callable=lambda values: [item.value for item in values],
            ),
            nullable=False,
            index=True,
        )
    )
    image_ref: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True, index=True),
    )
    descriptor: dict[str, Any] = Field(
        sa_column=Column(JSONB().with_variant(JSON(), "sqlite"), nullable=False)
    )
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
