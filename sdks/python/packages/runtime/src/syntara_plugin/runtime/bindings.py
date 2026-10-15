"""Transport-independent bindings of typed Python step implementations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal
from weakref import ReferenceType, ref

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012
from syntara_plugin.contracts import canonical_json_digest

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
    __ior__ = _reject_mutation  # type: ignore[method-assign]


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
    __iadd__ = _reject_mutation  # type: ignore[method-assign]
    __imul__ = _reject_mutation  # type: ignore[method-assign]


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
    if isinstance(value, tuple):
        return tuple(_freeze_json_document(item) for item in value)
    return value


def _validate_schema_references(schema: Mapping[str, Any]) -> None:
    resource = Resource.from_contents(schema, default_specification=DRAFT202012)
    _validate_resource_references(
        resource,
        Registry().resolver_with_root(resource),
    )


def _validate_resource_references(resource: Resource[Any], resolver: Any) -> None:
    contents = resource.contents
    if not isinstance(contents, Mapping):
        return
    for keyword in ("$ref", "$dynamicRef"):
        reference = contents.get(keyword)
        if not isinstance(reference, str):
            continue
        try:
            resolver.lookup(reference)
        except Unresolvable as error:
            message = (
                f"manifest input_schema contains an unresolvable {keyword}: "
                f"{reference}"
            )
            raise StepBindingError(message) from error
    for subresource in resource.subresources():
        _validate_resource_references(
            subresource,
            resolver.in_subresource(subresource),
        )
    definitions = contents.get("$defs")
    if not isinstance(definitions, Mapping):
        return
    for definition in definitions.values():
        if not isinstance(definition, Mapping | bool):
            continue
        definition_resource = Resource.from_contents(
            definition,
            default_specification=DRAFT202012,
        )
        _validate_resource_references(
            definition_resource,
            resolver.in_subresource(definition_resource),
        )


class StepTaxonomy(StrEnum):
    """The four executable plugin step categories."""

    ACTION = "action"
    TASK = "task"
    WORKFLOW = "workflow"
    TRIGGER = "trigger"


class StepBindingError(ValueError):
    """Raised when a typed Python step implementation cannot be bound."""


@dataclass(frozen=True)
class _InstalledActionProvenance:
    """The canonical contract retained for a manifest resolved at installation."""

    manifest_ref: ReferenceType[InstalledActionManifest]
    step_identity: str
    input_schema: Mapping[str, Any]
    input_schema_digest: str


@dataclass(frozen=True)
class _InstalledActionDescriptorProvenance:
    """The canonical contract bound by the trusted descriptor factory."""

    descriptor_ref: ReferenceType[StepDescriptor]
    step_identity: str
    input_schema: Mapping[str, Any]
    input_schema_digest: str


def _schema_digest(schema: Mapping[str, Any]) -> str:
    """Digest a possibly frozen schema after converting it to plain JSON."""

    return canonical_json_digest(_thaw_json_document(schema))


def _schema_matches_digest(schema: Mapping[str, Any], expected: str) -> bool:
    try:
        return _schema_digest(schema) == expected
    except (TypeError, ValueError):
        return False


def _thaw_json_document(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json_document(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json_document(item) for item in value]
    return value


_INSTALLED_ACTION_PROVENANCE: list[_InstalledActionProvenance] = []
_INSTALLED_ACTION_DESCRIPTOR_PROVENANCE: list[_InstalledActionDescriptorProvenance] = []


def _installed_action_provenance(
    installed_action: InstalledActionManifest,
) -> _InstalledActionProvenance | None:
    """Return provenance only for a manifest resolved by the installer."""

    _INSTALLED_ACTION_PROVENANCE[:] = [
        provenance
        for provenance in _INSTALLED_ACTION_PROVENANCE
        if provenance.manifest_ref() is not None
    ]
    return next(
        (
            provenance
            for provenance in _INSTALLED_ACTION_PROVENANCE
            if provenance.manifest_ref() is installed_action
        ),
        None,
    )


def _installed_action_descriptor_provenance(
    descriptor: StepDescriptor,
) -> _InstalledActionDescriptorProvenance | None:
    """Return provenance only for a descriptor created by the trusted factory."""

    _INSTALLED_ACTION_DESCRIPTOR_PROVENANCE[:] = [
        provenance
        for provenance in _INSTALLED_ACTION_DESCRIPTOR_PROVENANCE
        if provenance.descriptor_ref() is not None
    ]
    return next(
        (
            provenance
            for provenance in _INSTALLED_ACTION_DESCRIPTOR_PROVENANCE
            if provenance.descriptor_ref() is descriptor
        ),
        None,
    )


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


def validate_step_binding(
    descriptor: StepDescriptor,
    step: BaseStep[Any, Any],
) -> None:
    """Validate a descriptor's structural association with an implementation.

    This deliberately permits implementation-derived Action descriptors for
    authoring and contract-drift validation. Runtime execution additionally
    requires the installed Action contract; see
    :func:`validate_executable_step_binding`.
    """

    if type(descriptor) is not StepDescriptor:
        message = "only exact StepDescriptor instances can be bound"
        raise StepBindingError(message)
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


def validate_executable_step_binding(
    descriptor: StepDescriptor,
    step: BaseStep[Any, Any],
) -> None:
    """Validate a binding at an execution boundary.

    ``StepBinding`` is directly constructible and may not have come through
    ``StepBindings.bind``. In addition to structural validation, Action
    execution requires the exact descriptor created for an installed manifest.
    """

    validate_step_binding(descriptor, step)
    descriptor.require_executable_contract()


@dataclass(frozen=True)
class InstalledActionManifest:
    """An installed Action manifest and its resolved input schema.

    ``from_manifest`` is the SDK's local installation/bootstrap boundary. Its
    caller is responsible for obtaining an authorized installed document; an
    in-process SDK cannot independently establish remote or cryptographic
    provenance. The factory retains opaque provenance for the exact returned
    object so equivalent caller-created objects cannot be substituted into an
    executable Action binding.
    """

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
        installed_action = InstalledActionManifest()
        object.__setattr__(installed_action, "step_identity", step_identity)
        object.__setattr__(installed_action, "document", _freeze_json_document(manifest))
        object.__setattr__(installed_action, "input_schema", _freeze_json_document(input_schema))
        _INSTALLED_ACTION_PROVENANCE.append(
            _InstalledActionProvenance(
                manifest_ref=ref(installed_action),
                step_identity=installed_action.step_identity,
                input_schema=installed_action.input_schema,
                input_schema_digest=_schema_digest(input_schema),
            )
        )
        return installed_action


@dataclass(frozen=True)
class StepDescriptor:
    """Declared identity and schemas associated with a Python implementation.

    The input and output schemas describe the typed Python contract of one
    implementation. They are implementation types derived from Pydantic models
    and are not replacement manifests. Installed Actions are the exception: the
    trusted factory binds their canonical input schema. Schema-drift comparison
    between implementation schemas and source manifests is owned by a separate
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
        if self.input_schema_source != "implementation":
            message = (
                "manifest-sourced descriptors must be created with "
                "StepDescriptor.for_installed_action"
            )
            raise StepBindingError(message)
        object.__setattr__(self, "input_schema", _freeze_json_document(self.input_schema))
        object.__setattr__(self, "output_schema", _freeze_json_document(self.output_schema))

    @property
    def is_installed_action(self) -> bool:
        """Whether this descriptor was created from a trusted installed Action."""

        provenance = _installed_action_descriptor_provenance(self)
        return (
            provenance is not None
            and self.step_identity == provenance.step_identity
            and self.input_schema is provenance.input_schema
            and _schema_matches_digest(self.input_schema, provenance.input_schema_digest)
        )

    def require_executable_contract(self) -> None:
        """Reject bindings that cannot establish their required input contract."""

        if self.taxonomy is not StepTaxonomy.ACTION:
            return
        provenance = _installed_action_descriptor_provenance(self)
        if (
            provenance is None
            or self.step_identity != provenance.step_identity
            or self.input_schema is not provenance.input_schema
            or not _schema_matches_digest(self.input_schema, provenance.input_schema_digest)
        ):
            message = "the descriptor was not created from an unchanged installed contract"
            raise StepBindingError(message)

    @classmethod
    def for_step(cls, step_identity: str, step: BaseStep[Any, Any]) -> StepDescriptor:
        """Build an implementation-derived descriptor for a typed step."""

        taxonomy = _step_taxonomy(type(step))
        return StepDescriptor(
            step_identity=step_identity,
            taxonomy=taxonomy,
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
        if type(installed_action) is not InstalledActionManifest:
            message = "installed Action bindings require an exact InstalledActionManifest"
            raise TypeError(message)
        provenance = _installed_action_provenance(installed_action)
        if provenance is None:
            message = "installed Action bindings require a verified installed manifest"
            raise StepBindingError(message)
        if not _schema_matches_digest(
            installed_action.input_schema, provenance.input_schema_digest
        ):
            message = "the installed Action schema was changed after installation"
            raise StepBindingError(message)
        if _step_taxonomy(type(step)) is not StepTaxonomy.ACTION:
            message = "installed Action bindings require an ActionStep implementation"
            raise StepBindingError(message)
        descriptor = StepDescriptor(
            step_identity=provenance.step_identity,
            taxonomy=StepTaxonomy.ACTION,
            input_schema=provenance.input_schema,
            output_schema=step.output_model().model_json_schema(),
        )
        try:
            Draft202012Validator.check_schema(descriptor.input_schema)
            _validate_schema_references(descriptor.input_schema)
        except SchemaError as error:
            message = "manifest input_schema must be a valid JSON Schema"
            raise StepBindingError(message) from error
        object.__setattr__(descriptor, "input_schema_source", "manifest")
        _INSTALLED_ACTION_DESCRIPTOR_PROVENANCE.append(
            _InstalledActionDescriptorProvenance(
                descriptor_ref=ref(descriptor),
                step_identity=descriptor.step_identity,
                input_schema=descriptor.input_schema,
                input_schema_digest=provenance.input_schema_digest,
            )
        )
        return descriptor


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
            StepBindingError: If the descriptor is not a real
                exact ``StepDescriptor``, the step is not a ``BaseStep``, the
                identity is malformed or duplicate, or the descriptor taxonomy
                does not match the step's taxonomy class.
        """

        validate_step_binding(descriptor, step)
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
