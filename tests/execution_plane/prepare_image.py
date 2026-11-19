"""Prepare isolated image contexts from the checkout without running build hooks."""

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
CREDENTIAL_WORKLOAD = ROOT / "tests/execution_plane/credential_workload"


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


def _workload_sources(workload: str) -> tuple[Path, str, str]:
    """Return the checkout source, package, and workload ID for one image."""
    if workload == "existing-step":
        return EXAMPLE, "existing_step", "existing-step-workload"
    if workload == "credential-steps":
        return CREDENTIAL_WORKLOAD, "credential_steps", "credential-steps-workload"
    raise ValueError(f"unknown workload {workload!r}")


def _reject_symlinked_sources(workload: str) -> None:
    """Reject symlinks in every checkout path copied into the image context."""
    source, package, _ = _workload_sources(workload)
    sources = [
        CONTRACTS / "src" / "syntara_plugin",
        source / "src" / package,
        *(ROOT / "contracts" / directory for directory in ("schemas", "fixtures", "proto")),
        source / "runtime.json",
        source / "Containerfile",
    ]
    for source_path in sources:
        for candidate in (source_path, *source_path.parents):
            if candidate.is_symlink():
                raise ValueError(f"Staged source contains a symlink: {candidate}")
            if candidate == ROOT:
                break
        if source_path.is_dir():
            for nested in source_path.rglob("*"):
                if nested.is_symlink():
                    raise ValueError(f"Staged source contains a symlink: {nested}")


def prepare(context: Path) -> dict[str, object]:
    """Stage the Existing Step example image context."""
    return _prepare_workload(context, "existing-step")


def prepare_credential_workload(context: Path) -> dict[str, object]:
    """Stage the credential fixture workload image context."""
    return _prepare_workload(context, "credential-steps")


def _prepare_workload(context: Path, workload: str) -> dict[str, object]:
    """Stage source trees, generate the registration and build both wheels."""
    source, package, workload_id = _workload_sources(workload)
    _reject_symlinked_sources(workload)
    context.mkdir(parents=True, exist_ok=False)
    wheels = context / "wheels"
    wheels.mkdir()
    projects = context / "projects"
    projects.mkdir()
    # Use the checkout's locked hashes for every third-party runtime dependency.
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--package",
            "syntara-plugin-contracts",
            "--no-default-groups",
            "--no-emit-workspace",
            "--no-header",
            "--output-file",
            str(context / "requirements.lock"),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    for project_source, project_package in ((CONTRACTS, "syntara_plugin"), (source, package)):
        metadata = tomllib.loads((project_source / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
        project = projects / project_package
        project.mkdir()
        shutil.copytree(
            project_source / "src" / project_package,
            project / "src" / project_package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        if project_source == CONTRACTS:
            for directory in ("schemas", "fixtures", "proto"):
                shutil.copytree(
                    ROOT / "contracts" / directory,
                    project / "src" / project_package / "contracts" / directory,
                )
        write_project(
            project, metadata["name"], metadata["version"], metadata["dependencies"], project_package
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

    manifest = registration_manifest(
        _compile_descriptor(projects, source / "src" / package / "manifests" / "plugin.yaml"),
        workload_id,
    )
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
    shutil.copyfile(source / "runtime.json", context / "runtime.json")
    certificates = context / "certificates"
    certificates.mkdir()
    _generate_tls_material(certificates)
    shutil.copyfile(
        ROOT / "tests/execution_plane/runtime_entrypoint.py", context / "runtime_entrypoint.py"
    )
    shutil.copyfile(ROOT / "tests/execution_plane/tls_bridge.py", context / "tls_bridge.py")
    _write_compat_containerfile(context, source)
    return manifest


def _compile_descriptor(projects: Path, plugin_manifest: Path) -> dict[str, object]:
    """Compile one workspace, supplying only checkout distribution metadata.

    The compiler requires installed distribution metadata for provenance. Do
    not install the local SDK or run its hooks.
    """
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
        return compile_workspace(
            BuildRequest(plugin_manifest, require_image_bindings=False)
        ).descriptor
    finally:
        sys.path.remove(str(sdk_metadata))


def _write_compat_containerfile(context: Path, source: Path) -> None:
    """Rewrite the workload Containerfile for the compatibility image.

    EP's Kubernetes port-forward uses loopback and supplies no resolved file
    yet. These generated bindings are only a compatibility fixture, not a
    release image.
    """
    containerfile = (source / "Containerfile").read_text(encoding="utf-8")
    original_install = (
        "RUN python3.12 -m pip install --no-cache-dir --only-binary :all: /wheels/*.whl"
    )
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
        # Stage the certificates under /opt/syntara and override the TLS env
        # vars the base Containerfile points at /run/secrets: some build
        # engines (buildah/podman build) reserve /run/secrets in build
        # containers and silently drop anything copied there.
        + "COPY certificates/ /opt/syntara/certificates/\n"
        + "COPY runtime_entrypoint.py tls_bridge.py /opt/syntara/\n"
        + "RUN chown -R 1001:0 /opt/syntara/certificates "
        + "&& chmod 0600 /opt/syntara/certificates/*.key\n"
        + "ENV SYNTARA_RESOLVED_EXECUTION=/opt/syntara/resolved.json \\\n"
        + "    SYNTARA_RUNTIME_GRPC_ADDRESS=127.0.0.1:50052 \\\n"
        + "    SYNTARA_RUNTIME_GRPC_TLS_CERT=/opt/syntara/certificates/server.crt \\\n"
        + "    SYNTARA_RUNTIME_GRPC_TLS_KEY=/opt/syntara/certificates/server.key \\\n"
        + "    SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA=/opt/syntara/certificates/client-ca.crt\n"
        + "USER 1001\n"
        + 'ENTRYPOINT ["python3.12", "/opt/syntara/runtime_entrypoint.py"]\n',
        encoding="utf-8",
    )


def _generate_tls_material(directory: Path) -> None:
    """Create short-lived localhost mTLS material for the compatibility image."""
    ca_key = directory / "ca.key"
    ca_certificate = directory / "client-ca.crt"
    subprocess.run(["openssl", "genrsa", "-out", str(ca_key), "2048"], check=True)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-new",
            "-key",
            str(ca_key),
            "-out",
            str(ca_certificate),
            "-subj",
            "/CN=syntara-compat-ca",
            "-days",
            "1",
        ],
        check=True,
    )
    requests: list[Path] = []
    for name in ("server", "client"):
        key = directory / f"{name}.key"
        request = directory / f"{name}.csr"
        requests.append(request)
        certificate = directory / f"{name}.crt"
        subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True)
        subprocess.run(
            [
                "openssl",
                "req",
                "-new",
                "-key",
                str(key),
                "-out",
                str(request),
                "-subj",
                f"/CN={name if name == 'client' else 'localhost'}",
            ],
            check=True,
        )
        subprocess.run(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(request),
                "-CA",
                str(ca_certificate),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-out",
                str(certificate),
                "-days",
                "1",
            ],
            check=True,
        )
    for path in (ca_key, *requests, directory / "client-ca.srl"):
        path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("context", type=Path)
    parser.add_argument(
        "workload",
        nargs="?",
        default="existing-step",
        choices=("existing-step", "credential-steps"),
        help="which checkout workload to stage",
    )
    args = parser.parse_args()
    context = args.context.resolve()
    if args.workload == "credential-steps":
        prepare_credential_workload(context)
    else:
        prepare(context)


if __name__ == "__main__":
    main()
