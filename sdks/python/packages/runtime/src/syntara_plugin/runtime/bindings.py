"""Transport-independent bindings of typed Python step implementations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .step import ActionStep, BaseStep, TaskStep, TriggerStep, WorkflowStep

_IDENTITY = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")


class _ImmutableJSONMapping(dict[str, Any]):
    """A JSON object that rejects mutation through its public API."""

    def _reject_mutation(self, *_args: Any, **_kwargs: Any) -> Any:
        message = "installed schema documents are immutable"
        raise TypeError(message)

    __setitem__ = _reject_mutation  # type: ignore[method-assign]
    __delitem__ = _reject_mutation  # type: ignore[method-assign]
    clear = _reject_mutation  # type: ignore[method-assign]
    pop = _reject_mutation  # type: ignore[method-assign]
    popitem = _reject_mutation  # type: ignore[method-assign]
    setdefault = _reject_mutation  # type: ignore[method-assign]
    update = _reject_mutation  # type: ignore[method-assign]


class _ImmutableJSONList(list[Any]):
    """A JSON array that rejects mutation through its public API."""

    def _reject_mutation(self, *_args: Any, **_kwargs: Any) -> None:
        message = "installed schema documents are immutable"
        raise TypeError(message)

    __setitem__ = _reject_mutation  # type: ignore[method-assign]
    __delitem__ = _reject_mutation  # type: ignore[method-assign]
    append = _reject_mutation  # type: ignore[method-assign]
    extend = _reject_mutation  # type: ignore[method-assign]
    insert = _reject_mutation  # type: ignore[method-assign]
    pop = _reject_mutation  # type: ignore[method-assign]
    remove = _reject_mutation  # type: ignore[method-assign]
    sort = _reject_mutation  # type: ignore[method-assign]
    reverse = _reject_mutation  # type: ignore[method-assign]


def _freeze_json_document(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen = _ImmutableJSONMapping()
        dict.update(
            frozen,
            ((key, _freeze_json_document(item)) for key, item in value.items()),
        )
        return frozen
    if isinstance(value, list):
        return _ImmutableJSONList(_freeze_json_document(item) for item in value)
    return value


class StepTaxonomy(StrEnum):
    """The four executable plugin step categories."""

    ACTION = "action"
    TASK = "task"
    WORKFLOW = "workflow"
    TRIGGER = "trigger"


class StepBindingError(ValueError):
    """Raised when a typed Python step implementation cannot be bound."""


_TAXONOMY_CLASSES: tuple[tuple[type[BaseStep[Any, Any]], StepTaxonomy], ...] = (
    (ActionStep, StepTaxonomy.ACTION),
    (TaskStep, StepTaxonomy.TASK),
    (WorkflowStep, StepTaxonomy.WORKFLOW),
    (TriggerStep, StepTaxonomy.TRIGGER),
)


def _step_taxonomy(cls: type[BaseStep[Any, Any]]) -> StepTaxonomy:
    """Return the taxonomy of a concrete ``BaseStep`` subclass."""

    for taxonomy_class, taxonomy in _TAXONOMY_CLASSES:
        if issubclass(cls, taxonomy_class):
            return taxonomy
    message = f"{cls.__qualname__} does not belong to a step taxonomy class"
    raise StepBindingError(message)


@dataclass(frozen=True)
class InstalledActionManifest:
    """A trusted installed Action manifest and its resolved input schema."""

    step_identity: str = field(init=False)
    document: Mapping[str, Any] = field(init=False)
    input_schema: Mapping[str, Any] = field(init=False)

    @classmethod
    def from_manifest(
        cls,
        step_identity: str,
        manifest: Mapping[str, Any],
        assets: Mapping[str, Mapping[str, Any]],
    ) -> InstalledActionManifest:
        """Resolve the canonical schema declared by ``spec.input``."""

        if not _IDENTITY.fullmatch(step_identity):
            message = "step_identity must be a Syntara identity"
            raise StepBindingError(message)
        if not isinstance(manifest, Mapping):
            message = "manifest must be a JSON object"
            raise StepBindingError(message)
        if (
            manifest.get("apiVersion") != "syntara.io/v1alpha1"
            or manifest.get("kind") != "Action"
        ):
            message = "manifest must be a syntara.io/v1alpha1 Action document"
            raise StepBindingError(message)
        specification = manifest.get("spec")
        if not isinstance(specification, Mapping):
            message = "Action manifest must contain a spec object"
            raise StepBindingError(message)
        input_source = specification.get("input")
        if not isinstance(input_source, Mapping):
            message = "Action manifest must contain an input document source"
            raise StepBindingError(message)
        source_kind = input_source.get("kind")
        if source_kind == "inline":
            input_schema = input_source.get("value")
        elif source_kind == "asset":
            path = input_source.get("path")
            if not isinstance(path, str) or path not in assets:
                message = "Action input asset must be supplied by the installed binding"
                raise StepBindingError(message)
            input_schema = assets[path]
        else:
            message = "Action input must use an inline or asset document source"
            raise StepBindingError(message)
        if not isinstance(input_schema, Mapping):
            message = "resolved Action input schema must be a JSON object"
            raise StepBindingError(message)
        installed_action = cls()
        object.__setattr__(installed_action, "step_identity", step_identity)
        object.__setattr__(installed_action, "document", _freeze_json_document(manifest))
        object.__setattr__(installed_action, "input_schema", _freeze_json_document(input_schema))
        return installed_action


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

    step_identity: str
    taxonomy: StepTaxonomy
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    input_schema_source: Literal["implementation", "manifest"] = "implementation"

    def __post_init__(self) -> None:
        """Validate and normalize the descriptor's identity, taxonomy, and schemas."""

        if not isinstance(self.taxonomy, StepTaxonomy):
            try:
                object.__setattr__(self, "taxonomy", StepTaxonomy(self.taxonomy))
            except ValueError:
                message = "taxonomy must be a valid step taxonomy value"
                raise StepBindingError(message) from None
        if not _IDENTITY.fullmatch(self.step_identity):
            message = "step_identity must be a Syntara identity"
            raise StepBindingError(message)
        if not isinstance(self.input_schema, Mapping):
            message = "input_schema must be a JSON Schema mapping"
            raise StepBindingError(message)
        if not isinstance(self.output_schema, Mapping):
            message = "output_schema must be a JSON Schema mapping"
            raise StepBindingError(message)
        if self.input_schema_source not in {"implementation", "manifest"}:
            message = "input_schema_source must be 'implementation' or 'manifest'"
            raise StepBindingError(message)
        if self.input_schema_source == "manifest":
            object.__setattr__(self, "input_schema", _freeze_json_document(self.input_schema))
            try:
                Draft202012Validator.check_schema(self.input_schema)
            except SchemaError as error:
                message = "manifest input_schema must be a valid JSON Schema"
                raise StepBindingError(message) from error

    @classmethod
    def for_step(cls, step_identity: str, step: BaseStep[Any, Any]) -> StepDescriptor:
        """Build a descriptor from a step's taxonomy and Pydantic models."""

        return cls(
            step_identity=step_identity,
            taxonomy=_step_taxonomy(type(step)),
            input_schema=step.input_model().model_json_schema(),
            output_schema=step.output_model().model_json_schema(),
        )

    @classmethod
    def for_installed_action(
        cls,
        step: BaseStep[Any, Any],
        installed_action: InstalledActionManifest,
    ) -> StepDescriptor:
        """Build the trusted descriptor for one installed Action revision.

        ``input_schema`` must be the canonical schema resolved from the
        installed Action manifest. It is intentionally never derived from the
        implementation's Pydantic model.
        """

        if not isinstance(step, BaseStep):
            message = "installed Action bindings require a BaseStep implementation"
            raise TypeError(message)
        if _step_taxonomy(type(step)) is not StepTaxonomy.ACTION:
            message = "installed Action bindings require an ActionStep implementation"
            raise StepBindingError(message)
        return cls(
            step_identity=installed_action.step_identity,
            taxonomy=StepTaxonomy.ACTION,
            input_schema=installed_action.input_schema,
            output_schema=step.output_model().model_json_schema(),
            input_schema_source="manifest",
        )


@dataclass(frozen=True)
class StepBinding:
    """One immutable association between a descriptor and its implementation."""

    descriptor: StepDescriptor
    step: BaseStep[Any, Any]


class StepBindings:
    """Maps declared step identities to typed Python implementations.

    This is an in-process lookup table for direct SDK execution and local
    container dispatch. It is deliberately not the Automation Orchestrator's
    plugin or step registration database. Mapping an immutable installed step
    revision to one of these declared identities is owned by the separate SDK
    registry-binding story.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, StepBinding] = {}

    @property
    def step_identities(self) -> tuple[str, ...]:
        """Return the bound identities in stable sorted order."""

        return tuple(sorted(self._bindings))

    def bind(self, descriptor: StepDescriptor, step: BaseStep[Any, Any]) -> None:
        """Bind a descriptor to a typed step implementation.

        Raises:
            StepBindingError: If the step is not a ``BaseStep``, the
                identity is malformed or duplicate, or the descriptor taxonomy
                does not match the step's taxonomy class.
        """

        if not isinstance(step, BaseStep):
            message = (
                "only BaseStep implementations can be bound; platform-managed "
                "drivers such as http.v1 are declarative manifest runtimes, not "
                "Python implementations"
            )
            raise StepBindingError(message)
        step_taxonomy = _step_taxonomy(type(step))
        if step_taxonomy != descriptor.taxonomy:
            message = (
                f"descriptor taxonomy {descriptor.taxonomy.value!r} does not match "
                f"{type(step).__qualname__} taxonomy {step_taxonomy.value!r}"
            )
            raise StepBindingError(message)
        if descriptor.step_identity in self._bindings:
            message = f"step identity {descriptor.step_identity!r} is already bound"
            raise StepBindingError(message)
        self._bindings[descriptor.step_identity] = StepBinding(descriptor, step)

    def resolve(self, step_identity: str) -> BaseStep[Any, Any]:
        """Return the bound implementation for one step identity."""

        return self.binding(step_identity).step

    def binding(self, step_identity: str) -> StepBinding:
        """Return the bound descriptor and implementation for one identity."""

        try:
            return self._bindings[step_identity]
        except KeyError:
            message = f"step identity {step_identity!r} is not bound"
            raise StepBindingError(message) from None
