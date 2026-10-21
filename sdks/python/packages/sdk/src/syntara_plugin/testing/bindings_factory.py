"""Factory building drift-verified step bindings for local tests."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from syntara_plugin.runtime import (
    ActionStep,
    BaseStep,
    ControlStep,
    InstalledStepManifest,
    SchemaDriftError,
    StepBinding,
    StepBindingError,
    StepBindings,
    StepDescriptor,
    StepKind,
    TriggerStep,
    check_step_contract,
    check_target_binding,
)
from syntara_plugin.sdk import BuildRequest, CompilationResult, compile_workspace

from .errors import HarnessUsageError
from .loader import (
    StepTarget,
    default_step_identity,
    entry_point_of,
    load_step_class,
    resolve_step,
)


def _plain(value: Any) -> Any:
    """Deep-convert frozen compiled documents into plain JSON-compatible values."""

    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _synthetic_manifest(
    step: BaseStep[Any, Any],
    *,
    input_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a local installed manifest from an implementation's own schemas.

    Mirrors the runtime's own test pattern: the provenance gate requires every
    executable binding to come from an installed manifest, so local bindings
    are installed through the same trusted factory with schemas derived from
    (or explicitly supplied for) the implementation.
    """

    step_class = type(step)
    if issubclass(step_class, ActionStep):
        kind = StepKind.ACTION
    elif issubclass(step_class, ControlStep):
        kind = StepKind.CONTROL
    else:
        kind = StepKind.TRIGGER
    manifest: dict[str, Any] = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": kind.value,
        "metadata": {
            "name": "local",
            "displayName": "Local binding",
            "description": "Locally installed binding.",
        },
        "spec": {
            "runtime": {
                "kind": "custom-runner",
                "entryPoint": entry_point_of(step_class),
            },
            "input": {"kind": "inline", "value": _plain(input_schema)},
            "output": {"kind": "inline", "value": _plain(output_schema)},
        },
    }
    if kind is StepKind.CONTROL:
        manifest["spec"]["invocation"] = {"kind": "orchestration"}
    elif kind is StepKind.TRIGGER:
        manifest["spec"]["driver"] = "webhook.v1"
        manifest["spec"]["configuration"] = {"kind": "inline", "value": {"type": "object"}}
    return manifest


def installed_binding(
    step: BaseStep[Any, Any],
    *,
    identity: str | None = None,
    input_schema: Mapping[str, Any] | None = None,
    output_schema: Mapping[str, Any] | None = None,
) -> StepBinding:
    """Build one locally installed, executable binding for a step implementation.

    The manifest is synthetic but flows through the trusted
    ``InstalledStepManifest.from_manifest`` and ``StepDescriptor.for_installed_step``
    factories, so the provenance gate accepts it. Schemas default to the
    implementation's own Pydantic models; explicit schemas install a declared
    contract instead (used, for example, to exercise output-schema drift).
    """

    resolved_identity = identity or default_step_identity(step)
    step_class = type(step)
    if "<locals>" in step_class.__qualname__:
        message = (
            f"{step_class.__qualname__} is defined inside a function. The installed-step "
            "contract requires an importable module:Class entry point, so step classes "
            "must be defined at module level; move the class out of the enclosing function."
        )
        raise HarnessUsageError(message)
    declared_input = (
        input_schema if input_schema is not None else step.input_model().model_json_schema()
    )
    declared_output = (
        output_schema if output_schema is not None else step.output_model().model_json_schema()
    )
    manifest = _synthetic_manifest(
        step, input_schema=declared_input, output_schema=declared_output
    )
    installed = InstalledStepManifest.from_manifest(resolved_identity, manifest, {})
    descriptor = StepDescriptor.for_installed_step(step, installed)
    return StepBinding(descriptor, step)


def _installable_manifest(target: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the raw Step manifest shape from one compiled target.

    The compiler normalizes targets into a canonical form (top-level identity
    and resolved schemas); ``InstalledStepManifest.from_manifest`` consumes
    the raw manifest shape. Rebuilding from the compiled target keeps the
    validated canonical schemas as the installation source of truth instead of
    re-reading author files that may have changed after compilation.
    """

    runtime = target["runtime"]
    specification: dict[str, Any] = {
        "runtime": {"kind": "custom-runner", "entryPoint": runtime["entryPoint"]},
        "input": {"kind": "inline", "value": _plain(target["input"])},
        "output": {"kind": "inline", "value": _plain(target["output"])},
    }
    kind = str(target["kind"])
    for field_name in ("invocation", "driver"):
        if field_name in target:
            specification[field_name] = _plain(target[field_name])
    if "configuration" in target:
        specification["configuration"] = {
            "kind": "inline",
            "value": _plain(target["configuration"]),
        }
    if "error" in target:
        specification["error"] = {"kind": "inline", "value": _plain(target["error"])}
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": kind,
        "metadata": _plain(target["metadata"]),
        "spec": specification,
    }


class StepBindingsFactory:
    """Builds drift-verified ``StepBindings`` for direct and container invocation.

    ``bind`` installs a local manifest derived from the implementation's own
    schemas (or explicitly supplied ones) and verifies it with
    ``check_step_contract``; every bound step is executable through
    :func:`~syntara_plugin.testing.invoke_step`. ``bind_step`` compiles a
    plugin workspace, installs the compiled Step manifest through the same
    trusted factory, and verifies the target with ``check_target_binding``.
    """

    def __init__(self, *, strict: bool = False) -> None:
        self._bindings = StepBindings()
        self._entry_points: dict[str, str] = {}
        self._strict = strict

    @property
    def step_identities(self) -> tuple[str, ...]:
        """Return the bound identities in stable sorted order."""

        return self._bindings.step_identities

    def resolve(self, identity_or_entry_point: str) -> StepBinding:
        """Return the bound binding for one identity or ``module:Class`` entry point.

        A key containing a colon is tried as an entry point first, then as a
        literal identity, because Syntara identities may legally contain colons.
        """

        key = identity_or_entry_point
        identity = self._entry_points.get(key)
        if identity is not None:
            return self._bindings.binding(identity)
        try:
            return self._bindings.binding(key)
        except StepBindingError as error:
            if ":" not in key:
                message = f"step identity {key!r} has not been bound yet"
                raise HarnessUsageError(message) from error
            message = f"entry point {key!r} has not been bound yet"
            raise HarnessUsageError(message) from error

    def bind(
        self,
        step: StepTarget,
        *,
        identity: str | None = None,
        input_schema: Mapping[str, Any] | None = None,
        output_schema: Mapping[str, Any] | None = None,
    ) -> StepBinding:
        """Bind one step through a locally installed, drift-verified manifest.

        The binding is executable through
        :func:`~syntara_plugin.testing.invoke_step`. For compiled workspaces
            prefer :meth:`bind_step`, which installs the real manifest.

        Args:
            step: A step class or instance, or a ``module:Class`` entry point.
            identity: Step identity; derived from the class when omitted.
            input_schema: Optional declared input schema to install and verify
                against (defaults to the implementation's own model schema).
            output_schema: Optional declared output schema to install and
                verify against.
        """

        step_instance = resolve_step(step)
        binding = installed_binding(
            step_instance,
            identity=identity,
            input_schema=input_schema,
            output_schema=output_schema,
        )
        report = check_step_contract(
            step_instance,
            input_schema=binding.descriptor.input_schema,
            output_schema=binding.descriptor.output_schema,
            step_identity=binding.descriptor.step_identity,
        )
        report.raise_for_drift(strict=self._strict)
        self._register(binding.descriptor, step_instance)
        return binding

    def bind_step(
        self,
        step: StepTarget,
        *,
        workspace: Path | str,
        identity: str | None = None,
    ) -> StepBinding:
        """Compile a workspace, install its Step manifest, and bind the implementation.

        The workspace is compiled through the same canonical compiler the CLI
        ``validate`` command uses (with ``require_image_bindings=False`` since
        local tests build no images). The matching custom-runner Step
        target is located by entry point (or explicitly by ``identity``), its
        compiled manifest is installed through
        ``InstalledStepManifest.from_manifest``, and only that target is
        verified with ``check_target_binding`` so schema drift between the
        manifest and the implementation fails the test. Sibling targets are
        validated by their own ``bind_step`` calls; each call checks exactly
        the target it binds.

        Args:
            step: The implementation, as an entry point, class, or instance.
                When given as an entry-point string, that exact string is used
                to locate the target, so re-exported classes
                (``pkg:Class`` resolving to ``pkg.steps:Class``) match the
                manifest as written.
            workspace: Path to the root ``plugin.yaml`` or its directory.
            identity: Full step identity; located by entry point when omitted.
        """

        root = Path(workspace)
        if root.is_dir():
            root = root / "plugin.yaml"
        result = compile_workspace(
            BuildRequest(root_manifest=root, require_image_bindings=False)
        )
        step_instance = resolve_step(step)
        declared_entry_point = step if isinstance(step, str) else None
        entry_point = declared_entry_point or entry_point_of(type(step_instance))
        expected_kind = _step_kind(type(step_instance))
        target = self._find_step_target(
            result,
            entry_point=entry_point,
            identity=identity,
            expected_kind=expected_kind,
        )
        resolved_identity = identity or str(target["identity"])
        installed = InstalledStepManifest.from_manifest(
            resolved_identity, _installable_manifest(target), {}
        )
        descriptor = StepDescriptor.for_installed_step(step_instance, installed)
        candidate = StepBinding(descriptor, step_instance)
        # Validate only the bound target, before registering, so a failed drift
        # check never leaves an unverified binding resolvable in this factory
        # and sibling targets cannot fail a call that does not bind them.
        report = check_target_binding(
            target, candidate, entry_point_resolver=load_step_class
        )
        report.raise_for_drift(strict=self._strict)
        self._register(descriptor, step_instance)
        declared = str(target["runtime"]["entryPoint"])
        if declared != entry_point_of(type(step_instance)):
            # Re-exported entry points resolve by the string the manifest
            # declares, not only by the implementation's canonical path.
            self._entry_points[declared] = descriptor.step_identity
        return candidate

    def _register(self, descriptor: StepDescriptor, step_instance: BaseStep[Any, Any]) -> None:
        self._bindings.bind(descriptor, step_instance)
        self._entry_points[entry_point_of(type(step_instance))] = descriptor.step_identity

    def _find_step_target(
        self,
        result: CompilationResult,
        *,
        entry_point: str,
        identity: str | None,
        expected_kind: StepKind,
    ) -> Mapping[str, Any]:
        targets = result.descriptor["spec"]["targets"]
        lookup = f"identity {identity!r}" if identity is not None else f"entry point {entry_point!r}"
        if identity is not None:
            matches = [item for item in targets if str(item.get("identity")) == identity]
        else:
            matches = [
                item
                for item in targets
                if item.get("kind") == expected_kind.value
                and item.get("runtime", {}).get("entryPoint") == entry_point
            ]
        if not matches:
            available = ", ".join(
                f"{item.get('identity')} [{item.get('kind')}] ({item.get('runtime', {}).get('entryPoint')})"
                for item in targets
            )
            message = (
                f"no {expected_kind.value} target matches {lookup} in the compiled workspace; "
                f"compiled targets: {available or 'none'}"
            )
            raise HarnessUsageError(message)
        if len(matches) > 1:
            message = f"multiple {expected_kind.value} targets matched; pass identity= to select one"
            raise HarnessUsageError(message)
        target = matches[0]
        if target.get("kind") != expected_kind.value:
            message = (
                f"target {target.get('identity')!r} is {target.get('kind')!r}, "
                f"not {expected_kind.value!r}"
            )
            raise HarnessUsageError(message)
        runtime_kind = target.get("runtime", {}).get("kind")
        if runtime_kind != "custom-runner":
            message = (
                f"target {target.get('identity')!r} uses the {runtime_kind!r} runtime; "
                "only custom-runner Step targets bind Python implementations "
                "(platform-runner targets are declarative manifests)"
            )
            raise HarnessUsageError(message)
        matched: Mapping[str, Any] = target
        return matched


def _step_kind(step_class: type[BaseStep[Any, Any]]) -> StepKind:
    if issubclass(step_class, ActionStep):
        return StepKind.ACTION
    if issubclass(step_class, ControlStep):
        return StepKind.CONTROL
    if issubclass(step_class, TriggerStep):
        return StepKind.TRIGGER
    message = f"{step_class.__qualname__} does not belong to a supported Step kind"
    raise HarnessUsageError(message)


__all__ = [
    "HarnessUsageError",
    "SchemaDriftError",
    "StepBindingsFactory",
]
