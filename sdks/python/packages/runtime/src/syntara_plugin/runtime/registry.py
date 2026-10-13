"""Transport-independent registration of typed Python step implementations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .node import ActionStep, BaseNode, TaskStep, TriggerStep, WorkflowStep

_IDENTITY = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")


class StepTaxonomy(StrEnum):
    """The four executable plugin step categories."""

    ACTION = "action"
    TASK = "task"
    WORKFLOW = "workflow"
    TRIGGER = "trigger"


class StepRegistrationError(ValueError):
    """Raised when a typed Python step implementation cannot be registered."""


_TAXONOMY_CLASSES: tuple[tuple[type[BaseNode[Any, Any]], StepTaxonomy], ...] = (
    (ActionStep, StepTaxonomy.ACTION),
    (TaskStep, StepTaxonomy.TASK),
    (WorkflowStep, StepTaxonomy.WORKFLOW),
    (TriggerStep, StepTaxonomy.TRIGGER),
)


def _node_taxonomy(cls: type[BaseNode[Any, Any]]) -> StepTaxonomy:
    """Return the taxonomy of a concrete ``BaseNode`` subclass."""

    for taxonomy_class, taxonomy in _TAXONOMY_CLASSES:
        if issubclass(cls, taxonomy_class):
            return taxonomy
    message = f"{cls.__qualname__} does not belong to a step taxonomy class"
    raise StepRegistrationError(message)


@dataclass(frozen=True)
class StepDescriptor:
    """Declared identity and schemas associated with a Python implementation.

    The input and output schemas describe the typed Python contract of one
    implementation. They are implementation types derived from Pydantic models
    and are not replacement manifests: the canonical schema bundle remains the
    source of truth for manifest kinds such as Action and Trigger. Schema-drift
    comparison between these schemas and source manifests is owned by a separate
    story.
    """

    step_revision_id: str
    taxonomy: StepTaxonomy
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        """Validate the descriptor's identity and schema shapes."""

        if not _IDENTITY.fullmatch(self.step_revision_id):
            message = "step_revision_id must be a Syntara identity"
            raise StepRegistrationError(message)
        if not isinstance(self.input_schema, Mapping):
            message = "input_schema must be a JSON Schema mapping"
            raise StepRegistrationError(message)
        if not isinstance(self.output_schema, Mapping):
            message = "output_schema must be a JSON Schema mapping"
            raise StepRegistrationError(message)

    @classmethod
    def for_node(cls, step_revision_id: str, node: BaseNode[Any, Any]) -> StepDescriptor:
        """Build a descriptor from a node's taxonomy and Pydantic models."""

        return cls(
            step_revision_id=step_revision_id,
            taxonomy=_node_taxonomy(type(node)),
            input_schema=node.input_model().model_json_schema(),
            output_schema=node.output_model().model_json_schema(),
        )


class StepRegistry:
    """Maps declared step identities to typed Python implementations."""

    def __init__(self) -> None:
        self._nodes: dict[str, BaseNode[Any, Any]] = {}

    @property
    def step_revision_ids(self) -> tuple[str, ...]:
        """Return the registered identities in stable sorted order."""

        return tuple(sorted(self._nodes))

    def register(self, descriptor: StepDescriptor, node: BaseNode[Any, Any]) -> None:
        """Associate a descriptor with a typed step implementation.

        Raises:
            StepRegistrationError: If the node is not a ``BaseNode``, the
                identity is malformed or duplicate, or the descriptor taxonomy
                does not match the node's taxonomy class.
        """

        if not isinstance(node, BaseNode):
            message = (
                "only BaseNode implementations can be registered; platform-managed "
                "drivers such as http.v1 are declarative manifest runtimes, not "
                "Python implementations"
            )
            raise StepRegistrationError(message)
        node_taxonomy = _node_taxonomy(type(node))
        if node_taxonomy != descriptor.taxonomy:
            message = (
                f"descriptor taxonomy {descriptor.taxonomy.value!r} does not match "
                f"{type(node).__qualname__} taxonomy {node_taxonomy.value!r}"
            )
            raise StepRegistrationError(message)
        if descriptor.step_revision_id in self._nodes:
            message = f"step revision {descriptor.step_revision_id!r} is already registered"
            raise StepRegistrationError(message)
        self._nodes[descriptor.step_revision_id] = node

    def resolve(self, step_revision_id: str) -> BaseNode[Any, Any]:
        """Return the registered implementation for one step identity."""

        try:
            return self._nodes[step_revision_id]
        except KeyError:
            message = f"step revision {step_revision_id!r} is not registered"
            raise StepRegistrationError(message) from None
