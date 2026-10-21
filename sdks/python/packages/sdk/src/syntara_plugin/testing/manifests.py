"""Manifest validation and compilation helpers backed by the canonical bundle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from syntara_plugin.contracts import (
    ContractBundle,
    load_bundle,
    validate_document,
)
from syntara_plugin.contracts.bundle import ContractKind
from syntara_plugin.sdk import (
    BuildRequest,
    CompilationResult,
    Plugin,
    Target,
    compile_model,
    compile_workspace,
)

from .errors import HarnessUsageError


class ManifestValidationError(ValueError):
    """Raised when a manifest document fails canonical schema validation."""

    def __init__(self, kind: str, diagnostics: Sequence[str]) -> None:
        self.kind = kind
        self.diagnostics = tuple(diagnostics)
        details = "; ".join(self.diagnostics) or "the document is not valid"
        super().__init__(f"{kind} manifest failed validation: {details}")


def _resolve_kind(kind: str) -> str:
    """Normalize one declared or explicit kind to a contract kind name.

    Manifest kinds are CamelCase (``CredentialRecipe``, ``IntegrationType``)
    while contract kinds are snake_case (``credential_recipe``, ``integration``);
    both spellings are accepted.
    """

    lowered = kind.lower()
    return _KIND_ALIASES.get(lowered, lowered)


_KIND_ALIASES = {
    "action": "action",
    "trigger": "trigger",
    "plugin": "plugin",
    "provider": "provider",
    "runtime": "runtime",
    "credentialrecipe": "credential_recipe",
    "integrationtype": "integration",
    "catalogindex": "catalog_index",
    "catalogsource": "catalog_source",
    "catalogsourcestatus": "catalog_source_status",
    "httpoperation": "http_mapping",
    "httprunnercontext": "http_runner_context",
    "installedidentity": "installed_identity",
    "pluginbuildsettings": "plugin_build_settings",
    "triggerdriver": "trigger_driver",
    "triggereventmapping": "trigger_event_mapping",
}


#: The contract kinds the canonical bundle validates; anything else needs an
#: owner-supplied bundle.
_BUNDLE_KINDS: frozenset[ContractKind] = frozenset({
    "plugin", "plugin_build_settings", "action", "control", "trigger", "step",
    "plugin_descriptor", "runtime", "provider", "catalog_index", "catalog_source",
    "catalog_source_status", "integration", "http_mapping", "http_runner_context",
    "credential_recipe", "trigger_driver", "trigger_event_mapping", "installed_identity",
})


def _inferred_kind(document: Mapping[str, Any]) -> str:
    declared = document.get("kind")
    if isinstance(declared, str) and declared:
        return _resolve_kind(declared)
    message = "kind could not be inferred; pass kind= explicitly, for example kind='action'"
    raise ValueError(message)


class ManifestValidator:
    """Validate manifest documents against the canonical schema bundle.

    The validator never re-implements schemas: it calls the same
    ``syntara_plugin.contracts`` bundle the platform validates against at
    ingestion, or a step-type owner's canonical bundle supplied through
    ``bundle=`` together with its declared draft and version.
    """

    def __init__(self, *, bundle: ContractBundle | None = None) -> None:
        self._bundle = bundle or load_bundle()

    def check(
        self,
        document: Mapping[str, Any],
        *,
        kind: str | None = None,
        bundle: ContractBundle | None = None,
    ) -> list[str]:
        """Return the diagnostics for one document; empty when valid."""

        active = bundle or self._bundle
        resolved = _resolve_kind(kind) if kind is not None else _inferred_kind(document)
        if resolved not in _BUNDLE_KINDS:
            message = (
                f"kind {resolved!r} is not covered by the active bundle; kinds owned by "
                "step-type repositories need their canonical bundle supplied with "
                "bundle= (for example a Control or Trigger owner bundle)"
            )
            raise HarnessUsageError(message)
        try:
            return list(validate_document(active, resolved, document))
        except KeyError as error:
            message = (
                f"kind {resolved!r} is not covered by the active bundle; kinds owned by "
                "step-type repositories need their canonical bundle supplied with "
                "bundle= (for example a Task or Workflow owner bundle)"
            )
            raise HarnessUsageError(message) from error

    def __call__(
        self,
        document: Mapping[str, Any],
        *,
        kind: str | None = None,
        bundle: ContractBundle | None = None,
    ) -> None:
        """Raise :class:`ManifestValidationError` when one document is invalid."""

        diagnostics = self.check(document, kind=kind, bundle=bundle)
        if diagnostics:
            resolved = _resolve_kind(kind) if kind is not None else _inferred_kind(document)
            raise ManifestValidationError(resolved, diagnostics)


def compile_manifest(
    source: Path | str | Plugin,
    targets: Sequence[Target] | None = None,
    *,
    image_bindings: Mapping[str, str] | None = None,
    require_image_bindings: bool = False,
) -> CompilationResult:
    """Compile a plugin workspace path or an in-memory plugin model.

    ``source`` is either the path to a root ``plugin.yaml`` (or its containing
    directory) or an in-memory :class:`~syntara_plugin.sdk.Plugin` paired with
    ``targets``. Path compilation defaults to ``require_image_bindings=False``
    because local tests do not build workload images; pass ``image_bindings``
    when a releasable compile is wanted. In-memory compilation always requires
    image bindings for declared workloads, matching ``compile_model``.
    """

    if isinstance(source, Plugin):
        return compile_model(source, targets or (), image_bindings=image_bindings)
    root = Path(source)
    if root.is_dir():
        root = root / "plugin.yaml"
    return compile_workspace(
        BuildRequest(
            root_manifest=root,
            image_bindings=image_bindings,
            require_image_bindings=require_image_bindings,
        )
    )
