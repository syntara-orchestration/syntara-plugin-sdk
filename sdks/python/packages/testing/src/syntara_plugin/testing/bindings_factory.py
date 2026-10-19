"""Factory building drift-verified step bindings for local tests."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from syntara_plugin.runtime import (
    BaseStep,
    InstalledActionManifest,
    SchemaDriftError,
    StepBinding,
    StepBindings,
    StepDescriptor,
    check_step_contract,
    validate_plugin_bindings,
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


def _installable_manifest(target: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the raw Action manifest shape from one compiled target.

    The compiler normalizes targets into a canonical form (top-level identity
    and resolved schemas); ``InstalledActionManifest.from_manifest`` consumes
    the raw manifest shape. Rebuilding from the compiled target keeps the
    validated canonical schemas as the installation source of truth instead of
    re-reading author files that may have changed after compilation.
    """

    runtime = target["runtime"]
    specification: dict[str, Any] = {
        "runtime": {"kind": "custom-workload", "entryPoint": runtime["entryPoint"]},
        "input": {"kind": "inline", "value": _plain(target["input"])},
        "output": {"kind": "inline", "value": _plain(target["output"])},
    }
    if "error" in target:
        specification["error"] = {"kind": "inline", "value": _plain(target["error"])}
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Action",
        "metadata": _plain(target["metadata"]),
        "spec": specification,
    }


class StepBindingsFactory:
    """Builds drift-verified ``StepBindings`` for direct and container invocation.

    ``bind`` covers Task, Workflow, and Python-bound Trigger steps through
    implementation-derived descriptors verified with ``check_step_contract``.
    ``bind_action`` compiles a plugin workspace, installs the compiled Action
    manifest through ``InstalledActionManifest.from_manifest``, binds the
    implementation, and verifies the full plugin with
    ``validate_plugin_bindings``. Action execution requires that installed
    provenance; an Action bound with ``bind`` is usable for drift checks only.
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
        """Return the bound binding for one identity or ``module:Class`` entry point."""

        if ":" in identity_or_entry_point:
            identity = self._entry_points.get(identity_or_entry_point)
            if identity is None:
                message = f"entry point {identity_or_entry_point!r} has not been bound yet"
                raise HarnessUsageError(message)
        else:
            identity = identity_or_entry_point
        return self._bindings.binding(identity)

    def bind(
        self,
        step: StepTarget,
        *,
        identity: str | None = None,
        input_schema: Mapping[str, Any] | None = None,
        output_schema: Mapping[str, Any] | None = None,
    ) -> StepBinding:
        """Bind one step with an implementation-derived, drift-verified descriptor.

        Task, Workflow, and Trigger steps bound here are executable through
        :func:`~syntara_plugin.testing.invoke_step`. Binding an Action this way
        is allowed for drift validation but is not executable; use
        :meth:`bind_action` for Actions.

        Args:
            step: A step class or instance, or a ``module:Class`` entry point.
            identity: Step identity; derived from the class when omitted.
            input_schema: Optional declared input schema to verify against
                (defaults to the implementation's own model schema).
            output_schema: Optional declared output schema to verify against.
        """

        step_instance = resolve_step(step)
        resolved_identity = identity or default_step_identity(step_instance)
        base = StepDescriptor.for_step(resolved_identity, step_instance)
        descriptor = StepDescriptor(
            step_identity=resolved_identity,
            taxonomy=base.taxonomy,
            input_schema=input_schema or base.input_schema,
            output_schema=output_schema or base.output_schema,
        )
        report = check_step_contract(
            step_instance,
            input_schema=descriptor.input_schema,
            output_schema=descriptor.output_schema,
            step_identity=resolved_identity,
        )
        report.raise_for_drift(strict=self._strict)
        self._register(descriptor, step_instance)
        return StepBinding(descriptor, step_instance)

    def bind_action(
        self,
        step: StepTarget,
        *,
        workspace: Path | str,
        identity: str | None = None,
    ) -> StepBinding:
        """Compile a workspace, install its Action manifest, and bind the implementation.

        The workspace is compiled through the same canonical compiler the CLI
        ``validate`` command uses (with ``require_image_bindings=False`` since
        local tests build no images). The matching custom-workload Action
        target is located by entry point (or explicitly by ``identity``), its
        compiled manifest is installed through
        ``InstalledActionManifest.from_manifest``, and the resulting binding is
        verified with ``validate_plugin_bindings`` so schema drift between the
        manifest and the implementation fails the test.

        Args:
            step: The implementation, as an entry point, class, or instance.
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
        entry_point = entry_point_of(type(step_instance))
        target = self._find_action_target(result, entry_point=entry_point, identity=identity)
        resolved_identity = identity or str(target["identity"])
        installed = InstalledActionManifest.from_manifest(
            resolved_identity, _installable_manifest(target), {}
        )
        descriptor = StepDescriptor.for_installed_action(step_instance, installed)
        self._register(descriptor, step_instance)
        validate_plugin_bindings(
            result.descriptor,
            self._declared_view(result.descriptor),
            entry_point_resolver=load_step_class,
            strict=self._strict,
        )
        return StepBinding(descriptor, step_instance)

    def _register(self, descriptor: StepDescriptor, step_instance: BaseStep[Any, Any]) -> None:
        self._bindings.bind(descriptor, step_instance)
        self._entry_points[entry_point_of(type(step_instance))] = descriptor.step_identity

    def _declared_view(self, descriptor: Mapping[str, Any]) -> StepBindings:
        """Build a bindings view limited to identities this descriptor declares."""

        declared = {
            str(target["identity"]) for target in descriptor["spec"]["targets"]
        }
        view = StepBindings()
        for step_identity in self._bindings.step_identities:
            if step_identity in declared:
                binding = self._bindings.binding(step_identity)
                view.bind(binding.descriptor, binding.step)
        return view

    def _find_action_target(
        self,
        result: CompilationResult,
        *,
        entry_point: str,
        identity: str | None,
    ) -> Mapping[str, Any]:
        targets = result.descriptor["spec"]["targets"]
        if identity is not None:
            matches = [item for item in targets if str(item.get("identity")) == identity]
        else:
            matches = [
                item
                for item in targets
                if item.get("kind") == "Action"
                and item.get("runtime", {}).get("entryPoint") == entry_point
            ]
        if not matches:
            available = ", ".join(
                f"{item.get('identity')} ({item.get('runtime', {}).get('entryPoint')})"
                for item in targets
            )
            message = (
                f"no Action target matches entry point {entry_point!r} in the compiled "
                f"workspace; compiled targets: {available or 'none'}"
            )
            raise HarnessUsageError(message)
        if len(matches) > 1:
            message = "multiple Action targets matched; pass identity= to select one"
            raise HarnessUsageError(message)
        target = matches[0]
        if target.get("kind") != "Action":
            message = f"target {target.get('identity')!r} is not an Action"
            raise HarnessUsageError(message)
        return target


__all__ = [
    "HarnessUsageError",
    "SchemaDriftError",
    "StepBindingsFactory",
]
