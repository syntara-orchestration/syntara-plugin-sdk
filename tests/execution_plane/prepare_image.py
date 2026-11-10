"""Prepare an isolated image context from the checkout without running build hooks."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from syntara_plugin.contracts import registration_manifest
from syntara_plugin.sdk.authoring import BuildRequest, compile_workspace

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/existing-step"
CONTRACTS = ROOT / "sdks/python/packages/contracts"


def write_project(
    path: Path, name: str, version: str, dependencies: list[str], package: str
) -> None:
    """Use a generated build definition, never the checkout's build hooks."""
    (path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling==1.32.4"]\nbuild-backend = "hatchling.build"\n'
        f"\n[project]\nname = {json.dumps(name)}\nversion = {json.dumps(version)}\n"
        f'requires-python = ">=3.12"\ndependencies = {json.dumps(dependencies)}\n'
        f"\n[tool.hatch.build.targets.wheel]\npackages = [{json.dumps('src/' + package)}]\n",
        encoding="utf-8",
    )


def prepare(context: Path) -> dict[str, object]:
    """Stage source trees, generate the registration and build both wheels."""
    context.mkdir(parents=True, exist_ok=False)
    wheels = context / "wheels"
    wheels.mkdir()
    projects = context / "projects"
    projects.mkdir()
    # Use the checkout's locked hashes for every third-party runtime dependency.
    subprocess.run(
        [
            "uv", "export", "--locked", "--package", "syntara-plugin-contracts",
            "--no-default-groups", "--no-emit-workspace", "--no-header",
            "--output-file", str(context / "requirements.lock"),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    for source, package in ((CONTRACTS, "syntara_plugin"), (EXAMPLE, "existing_step")):
        metadata = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        project = projects / package
        project.mkdir()
        shutil.copytree(
            source / "src" / package,
            project / "src" / package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        if source == CONTRACTS:
            for directory in ("schemas", "fixtures", "proto"):
                shutil.copytree(
                    ROOT / "contracts" / directory,
                    project / "src" / package / "contracts" / directory,
                )
        write_project(
            project, metadata["name"], metadata["version"], metadata["dependencies"], package
        )
        subprocess.run(
            [
                "uv",
                "build",
                "--directory",
                str(project),
                "--wheel",
                "--no-build-isolation",
                "--out-dir",
                str(wheels),
                "--no-create-gitignore",
            ],
            check=True,
        )

    # The compiler requires installed distribution metadata for provenance. Supply
    # only checkout version metadata; do not install the local SDK or run its hooks.
    sdk_metadata = projects / "metadata"
    dist_info = sdk_metadata / "syntara_plugin_sdk-0.dist-info"
    dist_info.mkdir(parents=True)
    sdk_project = tomllib.loads(
        (ROOT / "sdks/python/packages/sdk/pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: syntara-plugin-sdk\nVersion: {sdk_project['version']}\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(sdk_metadata))
    try:
        descriptor = compile_workspace(
            BuildRequest(
                EXAMPLE / "src/existing_step/manifests/plugin.yaml", require_image_bindings=False
            )
        ).descriptor
    finally:
        sys.path.remove(str(sdk_metadata))
    manifest = registration_manifest(descriptor, "existing-step-workload")
    resolved = {
        "bindings": [
            {"stepRevisionId": revision, "runtimeKind": "custom-runner", **entry}
            for revision, entry in manifest["steps"].items()
        ]
    }
    (context / "registration.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    (context / "resolved.json").write_text(json.dumps(resolved, sort_keys=True), encoding="utf-8")
    shutil.copyfile(EXAMPLE / "runtime.json", context / "runtime.json")
    # EP's Kubernetes port-forward uses loopback and supplies no resolved file yet.
    # These generated bindings are only a compatibility fixture, not a release image.
    containerfile = (EXAMPLE / "Containerfile").read_text(encoding="utf-8")
    original_install = "RUN python3.12 -m pip install --no-cache-dir --only-binary :all: /wheels/*.whl"
    if containerfile.count(original_install) != 1:
        raise ValueError("Existing Step Containerfile install command changed")
    locked_install = (
        "COPY requirements.lock /tmp/requirements.lock\n"
        "RUN python3.12 -m pip download --only-binary :all: --require-hashes "
        "--dest /tmp/deps -r /tmp/requirements.lock && "
        "python3.12 -m pip install --no-index --find-links /tmp/deps --require-hashes "
        "-r /tmp/requirements.lock && "
        "python3.12 -m pip install --no-index --no-deps /wheels/*.whl && "
        "python3.12 -m pip check"
    )
    (context / "Containerfile").write_text(
        containerfile.replace(original_install, locked_install)
        + "\nUSER 0\nCOPY registration.json /opt/syntara/registration-manifest.json\n"
        + "COPY resolved.json /opt/syntara/resolved.json\n"
        + "ENV SYNTARA_RESOLVED_EXECUTION=/opt/syntara/resolved.json \\\n"
        + "    SYNTARA_RUNTIME_GRPC_ADDRESS=127.0.0.1:50051 \\\n"
        + '    SYNTARA_RUNTIME_GRPC_TLS_CERT="" SYNTARA_RUNTIME_GRPC_TLS_KEY="" SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA=""\n'
        + "USER 1001\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("context", type=Path)
    args = parser.parse_args()
    prepare(args.context.resolve())


if __name__ == "__main__":
    main()
