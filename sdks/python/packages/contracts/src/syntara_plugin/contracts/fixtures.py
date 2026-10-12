"""Load the pinned, language-neutral contract conformance fixture corpus."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any

_FIXTURE_BUNDLE_VERSION = "syntara.catalog-fixtures/v1alpha1"
_MANIFEST_PATH = "catalog-fixture-bundle.json"


@dataclass(frozen=True)
class FixtureBundle:
    """The pinned fixture inputs a control-plane consumer must accept or reject."""

    contract_bundle_digest: str
    content_digest: str
    documents: Mapping[str, Mapping[str, Any]]
    catalog_cases: tuple[Mapping[str, Any], ...]
    document_cases: tuple[Mapping[str, Any], ...]

    def document(self, path: str) -> Mapping[str, Any]:
        """Return one manifest-listed fixture document by its relative path."""

        try:
            return self.documents[path]
        except KeyError as error:
            raise KeyError(f"fixture {path!r} is not listed in the fixture bundle") from error


def fixture_content_digest(documents: Mapping[str, Mapping[str, Any]]) -> str:
    """Return the stable digest for the manifest-listed fixture documents."""

    payload = {path: documents[path] for path in sorted(documents)}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def load_fixture_bundle() -> FixtureBundle:
    """Load and integrity-check the shipped fixture corpus without network access."""

    manifest = _read_fixture(_MANIFEST_PATH)
    if manifest.get("fixtureBundleVersion") != _FIXTURE_BUNDLE_VERSION:
        raise RuntimeError("fixture bundle has an unsupported version")
    contract_digest = manifest.get("contractBundleDigest")
    expected_content_digest = manifest.get("fixtureContentDigest")
    catalog_cases = _cases(manifest, "catalogCases")
    document_cases = _cases(manifest, "documentCases")
    if not isinstance(contract_digest, str) or not isinstance(expected_content_digest, str):
        raise RuntimeError("fixture bundle is missing required digest metadata")

    paths = {case["path"] for case in (*catalog_cases, *document_cases)}
    documents = {path: _read_fixture(path) for path in sorted(paths)}
    actual_content_digest = fixture_content_digest(documents)
    if actual_content_digest != expected_content_digest:
        raise RuntimeError("fixture bundle content digest does not match its manifest")
    return FixtureBundle(
        contract_bundle_digest=contract_digest,
        content_digest=actual_content_digest,
        documents=documents,
        catalog_cases=catalog_cases,
        document_cases=document_cases,
    )


def _cases(manifest: Mapping[str, Any], name: str) -> tuple[Mapping[str, Any], ...]:
    cases = manifest.get(name)
    if not isinstance(cases, list) or not cases:
        raise RuntimeError(f"fixture bundle has no {name}")
    normalized: list[Mapping[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("path"), str):
            raise RuntimeError(f"fixture bundle has an invalid {name} entry")
        _safe_fixture_path(case["path"])
        normalized.append(case)
    return tuple(normalized)


def _read_fixture(path: str) -> Mapping[str, Any]:
    relative = _safe_fixture_path(path)
    resource = _fixture_directory().joinpath(*relative.parts)
    if not resource.is_file():
        raise RuntimeError(f"fixture {path!r} is missing from the package")
    document = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise RuntimeError(f"fixture {path!r} must be a JSON object")
    return document


def _safe_fixture_path(path: str) -> PurePosixPath:
    relative = PurePosixPath(path)
    if not path or relative.is_absolute() or ".." in relative.parts or relative.suffix != ".json":
        raise ValueError("fixture paths must be contained JSON files")
    return relative


def _fixture_directory() -> Any:
    packaged = files("syntara_plugin.contracts").joinpath("fixtures")
    if packaged.is_dir():
        return packaged
    for parent in Path(__file__).resolve().parents:
        source = parent / "contracts" / "fixtures"
        if source.is_dir():
            return source
    raise RuntimeError("cannot locate packaged or repository contract fixtures")
