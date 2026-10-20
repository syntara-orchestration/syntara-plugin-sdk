"""Transport-independent bindings of typed Python step implementations."""

from __future__ import annotations

import math
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal
from weakref import ReferenceType, ref

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012
from syntara_plugin.contracts import canonical_json_digest

from .step import ActionStep, BaseStep, ControlStep, TriggerStep

_IDENTITY = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_ENTRY_POINT = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"
)


def _freeze_json_document(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON document object keys must be strings")
        return MappingProxyType(
            {key: _freeze_json_document(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_document(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError("JSON documents may contain only JSON-compatible values")


def _validate_schema_references(
    schema: Mapping[str, Any], schema_name: Literal["input_schema", "output_schema"]
) -> None:
    resource = Resource.from_contents(
        _thaw_json_document(schema), default_specification=DRAFT202012
    )
    _validate_resource_references(
        resource,
        Registry().resolver_with_root(resource),
        schema_name,
    )


def _validate_resource_references(
    resource: Resource[Any], resolver: Any, schema_name: Literal["input_schema", "output_schema"]
) -> None:
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
            message = f"manifest {schema_name} contains an unresolvable {keyword}: {reference}"
            raise StepBindingError(message) from error
    for subresource in resource.subresources():
        _validate_resource_references(
            subresource,
            resolver.in_subresource(subresource),
            schema_name,
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
            schema_name,
        )


class StepCategory(StrEnum):
    """The executable plugin Step categories."""

    ACTION = "action"
    CONTROL = "control"
    TRIGGER = "trigger"


class StepKind(StrEnum):
    """The concrete public Step types, distinct from their broad category."""

    ACTION = "Action"
    CONTROL = "Control"
    TRIGGER = "Trigger"


class StepBindingError(ValueError):
    """Raised when a typed Python step implementation cannot be bound."""


@dataclass(frozen=True)
class _InstalledStepProvenance:
    """The canonical contract retained for a manifest resolved at installation."""

    manifest_ref: ReferenceType[InstalledStepManifest]
    step_identity: str
    kind: StepKind
    entry_point: str
    input_schema: Mapping[str, Any]
    input_schema_digest: str
    output_schema: Mapping[str, Any]
    output_schema_digest: str


@dataclass(frozen=True)
class _InstalledStepDescriptorProvenance:
    """The canonical contract bound by the trusted descriptor factory."""

    descriptor_ref: ReferenceType[StepDescriptor]
    step_identity: str
    kind: StepKind
    input_schema_source: Literal["manifest"]
    entry_point: str
    input_schema: Mapping[str, Any]
    input_schema_digest: str
    output_schema: Mapping[str, Any]
    output_schema_digest: str


def _schema_digest(schema: Mapping[str, Any]) -> str:
    """Digest a possibly frozen schema after converting it to plain JSON."""

    return canonical_json_digest(_thaw_json_document(schema))


def _schema_matches_digest(schema: Mapping[str, Any], expected: str) -> bool:
    try:
        return _schema_digest(schema) == expected
    except (TypeError, ValueError):
        return False


def _step_entry_point(step: BaseStep[Any, Any]) -> str:
    """Return the canonical fully-qualified entry point for one step instance."""

    step_class = type(step)
    return f"{step_class.__module__}:{step_class.__qualname__}"


def _loaded_entry_point(entry_point: str) -> type[Any] | None:
    module_name, separator, qualname = entry_point.partition(":")
    if not separator or not qualname:
        return None
    module = sys.modules.get(module_name)
    if module is None:
        return None
    found: Any = module
    for part in qualname.split("."):
        found = getattr(found, part, None)
        if found is None:
            return None
    return found if isinstance(found, type) else None


def _require_installed_step_entry_point(
    entry_point: str,
    step: BaseStep[Any, Any],
) -> None:
    """Reject an implementation that differs from its installed manifest."""

    actual = _step_entry_point(step)
    loaded = _loaded_entry_point(entry_point)
    if loaded is type(step):
        return
    module_name, separator, _ = entry_point.partition(":")
    if (
        loaded is None
        and (not separator or module_name not in sys.modules)
        and entry_point == actual
    ):
        return
    message = (
        f"installed Step entry point {entry_point!r} does not match "
        f"the supplied step class {actual!r}"
    )
    raise StepBindingError(message)


def _thaw_json_document(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json_document(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json_document(item) for item in value]
    return value


def to_json_document(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a mutable plain-JSON copy of an immutable runtime document."""

    thawed = _thaw_json_document(value)
    if not isinstance(thawed, dict):
        raise TypeError("JSON documents must be object mappings")
    return thawed


_INSTALLED_STEP_PROVENANCE: list[_InstalledStepProvenance] = []
_INSTALLED_STEP_DESCRIPTOR_PROVENANCE: list[_InstalledStepDescriptorProvenance] = []


def _installed_step_provenance(
    installed_step: InstalledStepManifest,
) -> _InstalledStepProvenance | None:
    """Return provenance only for a manifest resolved by the installer."""

    _INSTALLED_STEP_PROVENANCE[:] = [
        provenance
        for provenance in _INSTALLED_STEP_PROVENANCE
        if provenance.manifest_ref() is not None
    ]
    return next(
        (
            provenance
            for provenance in _INSTALLED_STEP_PROVENANCE
            if provenance.manifest_ref() is installed_step
        ),
        None,
    )


def _installed_step_descriptor_provenance(
    descriptor: StepDescriptor,
) -> _InstalledStepDescriptorProvenance | None:
    """Return provenance only for a descriptor created by the trusted factory."""

    _INSTALLED_STEP_DESCRIPTOR_PROVENANCE[:] = [
        provenance
        for provenance in _INSTALLED_STEP_DESCRIPTOR_PROVENANCE
        if provenance.descriptor_ref() is not None
    ]
    return next(
        (
            provenance
            for provenance in _INSTALLED_STEP_DESCRIPTOR_PROVENANCE
            if provenance.descriptor_ref() is descriptor
        ),
        None,
    )


_STEP_CLASSES: tuple[tuple[type[BaseStep[Any, Any]], StepKind, StepCategory], ...] = (
    (ActionStep, StepKind.ACTION, StepCategory.ACTION),
    (ControlStep, StepKind.CONTROL, StepCategory.CONTROL),
    (TriggerStep, StepKind.TRIGGER, StepCategory.TRIGGER),
)

_CATEGORY_BY_KIND: Mapping[StepKind, StepCategory] = {
    StepKind.ACTION: StepCategory.ACTION,
    StepKind.CONTROL: StepCategory.CONTROL,
    StepKind.TRIGGER: StepCategory.TRIGGER,
}


def _category_for_kind(kind: StepKind) -> StepCategory:
    """Return the canonical category for a concrete Step kind."""

    return _CATEGORY_BY_KIND[kind]


def _step_kind_and_category(
    cls: type[BaseStep[Any, Any]],
) -> tuple[StepKind, StepCategory]:
    """Return the concrete Step kind and category for a ``BaseStep`` subclass."""

    for step_class, kind, category in _STEP_CLASSES:
        if issubclass(cls, step_class):
            return kind, category
    message = f"{cls.__qualname__} does not belong to a step category class"
    raise StepBindingError(message)


def validate_step_binding(
    descriptor: StepDescriptor,
    step: BaseStep[Any, Any],
) -> None:
    """Validate a descriptor's structural association with an implementation.

    This deliberately permits implementation-derived descriptors for authoring
    and contract-drift validation. Runtime execution additionally requires the
    installed custom-runner contract; see
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
    step_kind, step_category = _step_kind_and_category(type(step))
    if step_kind is not descriptor.kind:
        message = (
            f"descriptor kind {descriptor.kind.value!r} does not match "
            f"{type(step).__qualname__} kind {step_kind.value!r}"
        )
        raise StepBindingError(message)
    if step_category != descriptor.category:
        message = (
            f"descriptor category {descriptor.category.value!r} does not match "
            f"{type(step).__qualname__} category {step_category.value!r}"
        )
        raise StepBindingError(message)


def validate_executable_step_binding(
    descriptor: StepDescriptor,
    step: BaseStep[Any, Any],
) -> None:
    """Validate a binding at an execution boundary.

    ``StepBinding`` is directly constructible and may not have come through
    ``StepBindings.bind``. In addition to structural validation, executable
    custom-runner steps require the exact descriptor created for an installed
    manifest.
    """

    validate_step_binding(descriptor, step)
    descriptor.require_executable_contract(step)


def _resolve_document_source(
    specification: Mapping[str, Any],
    field_name: str,
    assets: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Resolve a ``spec.<field>`` inline or asset JSON Schema document source."""

    source = specification.get(field_name)
    if not isinstance(source, Mapping):
        message = f"Step manifest must contain a {field_name} document source"
        raise StepBindingError(message)
    source_kind = source.get("kind")
    if source_kind == "inline":
        schema = source.get("value")
    elif source_kind == "asset":
        path = source.get("path")
        if not isinstance(path, str) or path not in assets:
            message = f"Step {field_name} asset must be supplied by the installed binding"
            raise StepBindingError(message)
        schema = assets[path]
    else:
        message = f"Step {field_name} must use an inline or asset document source"
        raise StepBindingError(message)
    if not isinstance(schema, Mapping):
        message = f"resolved Step {field_name} schema must be a JSON object"
        raise StepBindingError(message)
    return schema


@dataclass(frozen=True)
class InstalledStepManifest:
    """An installed custom-runner Step manifest and its resolved schemas.

    ``from_manifest`` is the SDK's installation/bootstrap boundary. Its caller
    is responsible for obtaining an authorized installed document; the SDK
    cannot independently establish remote or cryptographic provenance. The
    factory retains opaque provenance for the exact returned object so
    equivalent caller-created objects cannot be substituted into an executable
    Step binding.
    """

    step_identity: str = field(init=False)
    kind: StepKind = field(init=False)
    document: Mapping[str, Any] = field(init=False)
    input_schema: Mapping[str, Any] = field(init=False)
    output_schema: Mapping[str, Any] = field(init=False)

    @classmethod
    def from_manifest(
        cls,
        step_identity: str,
        manifest: Mapping[str, Any],
        assets: Mapping[str, Mapping[str, Any]],
    ) -> InstalledStepManifest:
        """Resolve the canonical schemas declared by ``spec.input`` and ``spec.output``."""

        if not _IDENTITY.fullmatch(step_identity):
            message = "step_identity must be a Syntara identity"
            raise StepBindingError(message)
        if not isinstance(manifest, Mapping):
            message = "manifest must be a JSON object"
            raise StepBindingError(message)
        if manifest.get("apiVersion") != "syntara.io/v1alpha1":
            message = "manifest must be a syntara.io/v1alpha1 Step document"
            raise StepBindingError(message)
        try:
            kind = StepKind(manifest.get("kind"))
        except (TypeError, ValueError):
            message = "manifest kind must be Action, Control, or Trigger"
            raise StepBindingError(message) from None
        specification = manifest.get("spec")
        if not isinstance(specification, Mapping):
            message = "Step manifest must contain a spec object"
            raise StepBindingError(message)
        runtime = specification.get("runtime")
        if not isinstance(runtime, Mapping) or runtime.get("kind") != "custom-runner":
            message = "installed Step bindings require a custom-runner implementation"
            raise StepBindingError(message)
        entry_point = runtime.get("entryPoint")
        if not isinstance(entry_point, str) or _ENTRY_POINT.fullmatch(entry_point) is None:
            message = "installed Step bindings require a valid runtime entryPoint"
            raise StepBindingError(message)
        input_schema = _resolve_document_source(specification, "input", assets)
        output_schema = _resolve_document_source(specification, "output", assets)
        installed_step = InstalledStepManifest()
        object.__setattr__(installed_step, "step_identity", step_identity)
        object.__setattr__(installed_step, "kind", kind)
        object.__setattr__(installed_step, "document", _freeze_json_document(manifest))
        object.__setattr__(installed_step, "input_schema", _freeze_json_document(input_schema))
        object.__setattr__(installed_step, "output_schema", _freeze_json_document(output_schema))
        _INSTALLED_STEP_PROVENANCE.append(
            _InstalledStepProvenance(
                manifest_ref=ref(installed_step),
                step_identity=installed_step.step_identity,
                kind=installed_step.kind,
                entry_point=entry_point,
                input_schema=installed_step.input_schema,
                input_schema_digest=_schema_digest(input_schema),
                output_schema=installed_step.output_schema,
                output_schema_digest=_schema_digest(output_schema),
            )
        )
        return installed_step


@dataclass(frozen=True)
class StepDescriptor:
    """Declared identity and schemas associated with a Python implementation.

    The input and output schemas describe the typed Python contract of one
    implementation. They are implementation-derived for authoring descriptors.
    Installed custom-runner Steps are the executable exception: the trusted
    factory binds their canonical schemas from the installed descriptor.
    """

    step_identity: str
    kind: StepKind
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    input_schema_source: Literal["implementation", "manifest"] = "implementation"
    def __post_init__(self) -> None:
        """Validate and normalize the descriptor's identity, kind, and schemas."""

        if not isinstance(self.kind, StepKind):
            try:
                object.__setattr__(self, "kind", StepKind(self.kind))
            except (TypeError, ValueError):
                message = "kind must be a valid concrete Step kind"
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
                "StepDescriptor.for_installed_step"
            )
            raise StepBindingError(message)
        object.__setattr__(self, "input_schema", _freeze_json_document(self.input_schema))
        object.__setattr__(self, "output_schema", _freeze_json_document(self.output_schema))

    @property
    def category(self) -> StepCategory:
        """Return the category derived from the concrete Step kind."""

        return _category_for_kind(self.kind)

    @property
    def is_installed_step(self) -> bool:
        """Whether this descriptor came from a trusted installed Step."""

        provenance = _installed_step_descriptor_provenance(self)
        return (
            provenance is not None
            and self.step_identity == provenance.step_identity
            and self.kind is provenance.kind
            and self.input_schema_source == provenance.input_schema_source
            and self.input_schema is provenance.input_schema
            and _schema_matches_digest(self.input_schema, provenance.input_schema_digest)
            and self.output_schema is provenance.output_schema
            and _schema_matches_digest(self.output_schema, provenance.output_schema_digest)
        )

    def require_executable_contract(self, step: BaseStep[Any, Any]) -> None:
        """Reject bindings that cannot establish their required input contract."""

        provenance = _installed_step_descriptor_provenance(self)
        if (
            provenance is None
            or self.step_identity != provenance.step_identity
            or self.kind is not provenance.kind
            or self.input_schema_source != provenance.input_schema_source
            or self.input_schema is not provenance.input_schema
            or not _schema_matches_digest(self.input_schema, provenance.input_schema_digest)
            or self.output_schema is not provenance.output_schema
            or not _schema_matches_digest(self.output_schema, provenance.output_schema_digest)
        ):
            message = "the descriptor was not created from an unchanged installed contract"
            raise StepBindingError(message)
        _require_installed_step_entry_point(provenance.entry_point, step)

    @classmethod
    def for_step(cls, step_identity: str, step: BaseStep[Any, Any]) -> StepDescriptor:
        """Build an implementation-derived descriptor for a typed step."""

        kind, _ = _step_kind_and_category(type(step))
        return StepDescriptor(
            step_identity=step_identity,
            kind=kind,
            input_schema=step.input_model().model_json_schema(),
            output_schema=step.output_model().model_json_schema(),
        )

    @classmethod
    def for_installed_step(
        cls,
        step: BaseStep[Any, Any],
        installed_step: InstalledStepManifest,
    ) -> StepDescriptor:
        """Build the trusted descriptor for one installed custom-runner Step.

        ``input_schema`` and ``output_schema`` must be the canonical schemas
        resolved from the installed Step manifest. They are intentionally never
        derived from the implementation's Pydantic model.
        """

        if not isinstance(step, BaseStep):
            message = "installed Step bindings require a BaseStep implementation"
            raise TypeError(message)
        if type(installed_step) is not InstalledStepManifest:
            message = "installed Step bindings require an exact InstalledStepManifest"
            raise TypeError(message)
        provenance = _installed_step_provenance(installed_step)
        if provenance is None:
            message = "installed Step bindings require a verified installed manifest"
            raise StepBindingError(message)
        specification = installed_step.document.get("spec")
        runtime = specification.get("runtime") if isinstance(specification, Mapping) else None
        if not isinstance(runtime, Mapping) or runtime.get("kind") != "custom-runner":
            message = "installed Step bindings require a custom-runner runtime"
            raise StepBindingError(message)
        if runtime.get("entryPoint") != provenance.entry_point:
            message = "the installed Step entry point was changed after installation"
            raise StepBindingError(message)
        if not _schema_matches_digest(
            installed_step.input_schema, provenance.input_schema_digest
        ) or not _schema_matches_digest(
            installed_step.output_schema, provenance.output_schema_digest
        ):
            message = "the installed Step schema was changed after installation"
            raise StepBindingError(message)
        kind, _ = _step_kind_and_category(type(step))
        if kind is not installed_step.kind:
            message = (
                f"installed manifest kind {installed_step.kind.value!r} does not match "
                f"the supplied Step implementation kind {kind.value!r}"
            )
            raise StepBindingError(message)
        _require_installed_step_entry_point(provenance.entry_point, step)
        descriptor = StepDescriptor(
            step_identity=provenance.step_identity,
            kind=kind,
            input_schema=provenance.input_schema,
            output_schema=provenance.output_schema,
        )
        schemas: tuple[tuple[Literal["input_schema", "output_schema"], Mapping[str, Any]], ...] = (
            ("input_schema", descriptor.input_schema),
            ("output_schema", descriptor.output_schema),
        )
        for schema_name, schema in schemas:
            try:
                Draft202012Validator.check_schema(_thaw_json_document(schema))
            except SchemaError as error:
                message = f"manifest {schema_name} must be a valid JSON Schema"
                raise StepBindingError(message) from error
            _validate_schema_references(schema, schema_name)
        object.__setattr__(descriptor, "input_schema_source", "manifest")
        _INSTALLED_STEP_DESCRIPTOR_PROVENANCE.append(
            _InstalledStepDescriptorProvenance(
                descriptor_ref=ref(descriptor),
                step_identity=descriptor.step_identity,
                kind=descriptor.kind,
                input_schema_source="manifest",
                entry_point=provenance.entry_point,
                input_schema=descriptor.input_schema,
                input_schema_digest=provenance.input_schema_digest,
                output_schema=descriptor.output_schema,
                output_schema_digest=provenance.output_schema_digest,
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

    This is an SDK-local lookup table for direct execution and local
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
                identity is malformed or duplicate, or the descriptor kind or
                category does not match the step's concrete class.
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
