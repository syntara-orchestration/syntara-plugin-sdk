"""Command line tooling for scaffolding and packaging steps."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import yaml

from syntara_tools.compiler import (
    PluginDiscoveryError,
    compile_manifest,
    discover_plugin,
    load_manifest,
    validate_manifest,
)
from syntara_tools.oci_client import OCI_ARTIFACT_TYPE, OCI_MANIFEST_ANNOTATION, OCIRegistryClient

SHARED_SCRIPT_IMAGE = "quay.io/syntara/script-python-executor:latest"
SHARED_HTTP_IMAGE = "quay.io/syntara/http-request-executor:latest"
_STEP_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _manifest(name: str, tier: int, image: str) -> dict[str, Any]:
    # Every image-backed step declares a handle, single-step plugin or not, so
    # the runtime loads it the same way in all cases.
    step_class = _step_class_name(name)
    execution: dict[str, Any] = {"image": image, "entrypoint": f"main:{step_class}"}
    if tier == 1:
        execution = {"image": None, "entrypoint": None}
    return {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "StepType",
        "metadata": {
            "name": name,
            "displayName": name.replace("_", " ").title(),
            "icon": "terminal",
            "description": f"Custom tier {tier} step.",
            "tags": [],
            "license": "Apache-2.0",
        },
        "spec": {
            "category": "task",
            "execution": execution,
            "inputs": {"type": "object", "properties": {}, "required": []},
            "outputs": {
                "allOf": [
                    {
                        "$ref": "../../schemas/common-definitions.json#/definitions/StandardOutputWrapper"
                    }
                ]
            },
            "executionTimeout": 300,
        },
    }


def _step_class_name(name: str) -> str:
    """Derive the BaseStep subclass name the entrypoint will reference."""

    return "".join(part.title() for part in name.split("_")) + "Step"


def _step_module(class_name: str) -> str:
    """Scaffold a BaseStep subclass matching the declared entrypoint."""

    return f'''"""Step implementation. Loaded by the runtime via spec.execution.entrypoint."""

from pydantic import BaseModel

from syntara_sdk import ExecutionContext, TaskStep


class {class_name}Input(BaseModel):
    """Typed inputs. Keep in sync with spec.inputs in manifest.yaml."""


class {class_name}Output(BaseModel):
    """Inner Result payload; the base class wraps it in StandardOutputWrapper."""


class {class_name}(TaskStep[{class_name}Input, {class_name}Output]):
    def __init__(self) -> None:
        super().__init__({class_name}Input, {class_name}Output)

    def run(
        self, inputs: {class_name}Input, context: ExecutionContext
    ) -> {class_name}Output:
        raise NotImplementedError("implement the step logic")
'''


def _resolve_within(candidate: Path, base: Path) -> Path:
    """Resolve ``candidate`` and refuse to escape ``base``.

    Scaffolding paths may arrive from a CLI argument, which in an agentic
    workflow can be model-generated rather than typed by a person. A value
    like ``../../etc`` would otherwise write files outside the project, so the
    resolved target must stay inside the base directory.
    """

    base = base.resolve()
    target = (base / candidate).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"path escapes the base directory {base}: {candidate}")
    return target


def init_step(
    path: Path,
    name: str,
    tier: int,
    image: str | None,
    base_dir: Path | None = None,
) -> None:
    """Create a Tier 2 script package or Tier 3 image package."""

    if not _STEP_NAME.fullmatch(name):
        raise ValueError("name must be lowercase snake_case")
    if tier not in {2, 3}:
        raise ValueError("--tier must be 2 or 3")
    path = _resolve_within(path, base_dir if base_dir is not None else Path.cwd())
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(f"target directory is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)

    resolved_image = image or f"quay.io/example/{name}:0.1.0"
    manifest = _manifest(name, tier, resolved_image)
    (path / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    # Every step is built the same way: a BaseStep subclass whose name matches
    # spec.execution.entrypoint, so the runtime loads it identically whether
    # the plugin ships one step or many.
    (path / "main.py").write_text(_step_module(_step_class_name(name)))
    if tier == 3:
        entrypoint = manifest["spec"]["execution"]["entrypoint"]
        (path / "Containerfile").write_text(
            "FROM docker.io/library/python:3.12-slim\n"
            "RUN pip install --no-cache-dir syntara-sdk\n"
            "COPY main.py /app/main.py\n"
            "WORKDIR /app\n"
            "USER 1000:1000\n"
            # Launch through the SDK runner so the step is invoked via its
            # BaseStep subclass. A bare `python main.py` would bypass input
            # validation and the redact echo check.
            f'CMD ["python", "-m", "syntara_sdk.runner", "--entrypoint", "{entrypoint}"]\n'
        )


def _oci_manifest(manifest: dict[str, Any], image_ref: str) -> dict[str, Any]:
    """Return an OCI artifact manifest carrying the step YAML as an annotation."""

    # The artifact ref is where the plugin is published. It is unrelated to
    # spec.execution.image, which names the runtime the step executes in.
    if not isinstance(image_ref, str) or not image_ref:
        raise ValueError("publishing requires an artifact reference (--registry/--image)")
    raw_yaml = yaml.safe_dump(manifest, sort_keys=False)
    empty_config = b"{}"
    digest = "sha256:" + hashlib.sha256(empty_config).hexdigest()
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": OCI_ARTIFACT_TYPE,
        "config": {
            "mediaType": "application/vnd.oci.empty.v1+json",
            "digest": digest,
            "size": len(empty_config),
        },
        "layers": [],
        "annotations": {
            OCI_MANIFEST_ANNOTATION: raw_yaml,
            "org.opencontainers.image.title": manifest["metadata"]["name"],
            "org.opencontainers.image.ref.name": image_ref,
        },
    }


def build_step(manifest_path: Path, output: Path, image_ref: str) -> Path:
    """Validate a manifest and write the plugin artifact manifest JSON or layout."""

    manifest = compile_manifest(manifest_path)
    oci_manifest = _oci_manifest(manifest, image_ref)
    encoded = json.dumps(oci_manifest, indent=2, sort_keys=True).encode()

    if output.suffix == ".json":
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(encoded)
        return output

    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    config = b"{}"
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    blob_root = output / "blobs" / "sha256"
    blob_root.mkdir(parents=True, exist_ok=True)
    (blob_root / config_digest.removeprefix("sha256:")).write_bytes(config)
    (blob_root / digest.removeprefix("sha256:")).write_bytes(encoded)
    (output / "oci-layout").write_text(json.dumps({"imageLayoutVersion": "1.0.0"}, indent=2) + "\n")
    (output / "index.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "artifactType": OCI_ARTIFACT_TYPE,
                        "digest": digest,
                        "size": len(encoded),
                        "annotations": {
                            "org.opencontainers.image.ref.name": image_ref
                            or manifest["spec"]["execution"]["image"]
                        },
                    }
                ],
            },
            indent=2,
        )
        + "\n"
    )
    return output


class SyntaraRegistrationError(RuntimeError):
    """Raised when the platform catalog cannot accept a published step."""


def register_with_syntara(
    image_ref: str,
    api_url: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Register a published OCI step in Syntara's step catalog."""

    owns_client = client is None
    http_client = client or httpx.Client(timeout=10.0)
    url = f"{api_url.rstrip('/')}/api/v1/step-types"
    try:
        try:
            response = http_client.post(url, json={"image_ref": image_ref})
        except httpx.HTTPError as exc:
            raise SyntaraRegistrationError(f"could not reach Syntara at {url}: {exc}") from exc
        if response.is_error:
            raise SyntaraRegistrationError(
                f"Syntara registration failed with HTTP {response.status_code}: {response.text[:500]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SyntaraRegistrationError("Syntara registration returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SyntaraRegistrationError("Syntara registration returned a non-object response")
        return payload
    finally:
        if owns_client:
            http_client.close()


def push_step(
    manifest_path: Path,
    image_ref: str,
    *,
    registry_client: OCIRegistryClient | None = None,
    register_api_url: str | None = None,
    registration_client: httpx.Client | None = None,
) -> str:
    """Publish the plugin artifact and optionally register it with Syntara."""

    manifest = compile_manifest(manifest_path)
    # spec.execution.image is deliberately left untouched: it names the runtime
    # environment, not where this plugin artifact is published.
    oci_manifest = _oci_manifest(manifest, image_ref)
    owns_client = registry_client is None
    client = registry_client or OCIRegistryClient()
    try:
        digest = client.push_manifest(image_ref, oci_manifest)
    finally:
        if owns_client:
            client.close()
    if register_api_url:
        register_with_syntara(image_ref, register_api_url, client=registration_client)
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="syntara-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="scaffold a step package")
    init_parser.add_argument("name")
    init_parser.add_argument("--tier", type=int, choices=[2, 3], required=True)
    init_parser.add_argument("--path", type=Path, default=None)
    init_parser.add_argument("--image")

    validate_parser = subparsers.add_parser(
        "validate", help="validate a standalone manifest.yaml or root plugin.yaml"
    )
    validate_parser.add_argument("manifest", type=Path)

    build_parser = subparsers.add_parser("build", help="package a Tier 3 step as an OCI artifact")
    build_parser.add_argument("manifest", type=Path)
    build_parser.add_argument("--output", type=Path, default=Path("oci-layout"))
    build_parser.add_argument("--image", help="dedicated image reference override")

    push_parser = subparsers.add_parser("push", help="push a Tier 3 manifest to an OCI registry")
    push_parser.add_argument("manifest", type=Path, nargs="?", default=Path("manifest.yaml"))
    push_parser.add_argument(
        "--registry",
        required=True,
        help="destination image reference, e.g. localhost:5000/syntara/steps/my-step:1.0.0",
    )
    push_parser.add_argument(
        "--api-url",
        default=os.getenv("SYNTARA_API_URL", "http://localhost:5173"),
        help="Syntara API base URL used to register the pushed step",
    )
    push_parser.add_argument(
        "--skip-register",
        action="store_true",
        help="publish to the OCI registry without registering in Syntara",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init_step(
                args.path or Path(args.name),
                args.name,
                args.tier,
                args.image,
            )
            return 0
        if args.command == "validate":
            document = load_manifest(args.manifest)
            if document.get("kind") == "Plugin":
                descriptor = discover_plugin(args.manifest)
                print(f"validated plugin with {len(descriptor.steps)} step(s)")
                return 0
            errors = validate_manifest(document)
            if errors:
                raise ValueError("Manifest validation failed:\n" + "\n".join(errors))
            print("validated step manifest")
            return 0
        if args.command == "build":
            output = _resolve_within(args.output, Path.cwd())
            result = build_step(args.manifest, output, args.image)
            print(result)
        else:
            digest = push_step(
                args.manifest,
                args.registry,
                register_api_url=None if args.skip_register else args.api_url,
            )
            if digest:
                print(digest)
            if not args.skip_register:
                print(f"registered {args.registry} with {args.api_url}")
        return 0
    except (
        FileExistsError,
        FileNotFoundError,
        ValueError,
        PluginDiscoveryError,
        SyntaraRegistrationError,
        yaml.YAMLError,
    ) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
