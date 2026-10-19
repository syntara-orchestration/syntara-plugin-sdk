"""Manifest validation and compilation helpers backed by the canonical bundle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from syntara_plugin.contracts import ContractBundle, load_bundle, validate_document
from syntara_plugin.sdk import (
    BuildRequest,
    CompilationResult,
    Plugin,
    Target,
    compile_model,
    compile_workspace,
)


class ManifestValidationError(ValueError):
    """Raised when a manifest document fails canonical schema validation."""

    def __init__(self, kind: str, diagnostics: Sequence[str]) -> None:
        self.kind = kind
        self.diagnostics = tuple(diagnostics)
        details = "; ".join(self.diagnostics) or "the document is not valid"
        super().__init__(f"{kind} manifest failed validation: {details}")


def _inferred_kind(document: Mapping[str, Any]) -> str:
    declared = document.get("kind")
    if isinstance(declared, str) and declared:
        return declared.lower()
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
        resolved = kind or _inferred_kind(document)
        return list(validate_document(active, resolved, document))

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
            resolved = kind or _inferred_kind(document)
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
