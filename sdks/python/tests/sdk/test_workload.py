"""Tests for the bounded Podman workload-image adapter."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from syntara_plugin.contracts import (
    canonical_json_bytes,
    plugin_revision_id,
    registration_manifest,
    step_revision_id,
)
from syntara_plugin.sdk.workload import (
    WorkloadBuildError,
    WorkloadBuildRequest,
    build_and_push_workload,
)


def _descriptor():
    return json.loads(
        (Path(__file__).parents[4] / "contracts/fixtures/plugin-descriptor.valid.json").read_text()
    )


def _assert_workload_build_rejected(request: WorkloadBuildRequest, match: str) -> None:
    with pytest.raises(WorkloadBuildError, match=match):
        build_and_push_workload(request)


def test_podman_build_and_push_returns_only_a_registry_digest(tmp_path: Path, monkeypatch) -> None:
    """The compiler receives repository@digest, never the mutable local tag."""
    context = tmp_path / "context"
    context.mkdir()
    containerfile = context / "Containerfile"
    containerfile.write_text("FROM scratch\n", encoding="utf-8")
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "push":
            Path(command[3]).write_text("sha256:" + "b" * 64, encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("syntara_plugin.sdk.workload.subprocess.run", run)
    image = build_and_push_workload(
        WorkloadBuildRequest(
            engine="podman",
            context=context,
            containerfile=containerfile,
            repository="registry.example.test/acme/custom",
            tag="0.1.0",
            platform="linux/amd64",
            registration_descriptor=_descriptor(),
            workload_id="plugin-workload",
        )
    )

    assert image == "registry.example.test/acme/custom@sha256:" + "b" * 64
    assert commands[0][:9] == [
        "podman",
        "build",
        "--format",
        "oci",
        "--platform",
        "linux/amd64",
        "--file",
        str(containerfile),
        "--tag",
    ]
    staging_reference = commands[0][9]
    assert staging_reference.startswith("registry.example.test/acme/custom:syntara-stage-")
    assert commands[0][10] == str(context)
    assert commands[1][9] == "registry.example.test/acme/custom:0.1.0"
    assert commands[2][:3] == ["podman", "push", "--digestfile"]


def test_verbose_workload_output_uses_stderr_so_json_stdout_stays_clean(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The CLI can safely combine --verbose with --json."""
    context = tmp_path / "context"
    context.mkdir()
    containerfile = context / "Containerfile"
    containerfile.write_text("FROM scratch\n", encoding="utf-8")

    def run(command, **kwargs):
        assert kwargs["stdout"] is not None
        assert kwargs["stdin"] is not None
        if command[1] == "push":
            Path(command[3]).write_text("sha256:" + "c" * 64, encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("syntara_plugin.sdk.workload.subprocess.run", run)
    build_and_push_workload(
        WorkloadBuildRequest(
            engine="podman",
            context=context,
            containerfile=containerfile,
            repository="registry.example.test/acme/custom",
            tag="0.1.0",
            platform="linux/amd64",
            registration_descriptor=_descriptor(),
            workload_id="plugin-workload",
        ),
        verbose=True,
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "+ podman build" in captured.err
    assert "+ podman push" in captured.err


def test_registration_manifest_is_copied_into_final_image_before_push(
    tmp_path: Path, monkeypatch
) -> None:
    context = tmp_path / "context"
    context.mkdir()
    containerfile = context / "Containerfile"
    containerfile.write_text("FROM scratch\n", encoding="utf-8")
    commands: list[list[str]] = []
    descriptor = _descriptor()
    manifest = canonical_json_bytes(registration_manifest(descriptor, "plugin-workload"))

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "build" and len(commands) == 2:
            generated_context = Path(command[-1])
            assert (generated_context / "manifest.json").read_bytes() == manifest
            assert (generated_context / "Containerfile").read_text() == (
                f"FROM {commands[0][9]}\n"
                "COPY manifest.json /opt/syntara/registration-manifest.json\n"
            )
            assert commands[0][9] != "registry.example.test/acme/custom:0.1.0"
            assert commands[1][9] == "registry.example.test/acme/custom:0.1.0"
        if command[1] == "push":
            Path(command[3]).write_text("sha256:" + "b" * 64, encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("syntara_plugin.sdk.workload.subprocess.run", run)
    build_and_push_workload(
        WorkloadBuildRequest(
            engine="podman",
            context=context,
            containerfile=containerfile,
            repository="registry.example.test/acme/custom",
            tag="0.1.0",
            platform="linux/amd64",
            registration_descriptor=descriptor,
            workload_id="plugin-workload",
        )
    )
    assert [command[1] for command in commands] == ["build", "build", "push"]


def test_build_requires_valid_compiled_registration_before_running_podman(
    tmp_path: Path, monkeypatch
) -> None:
    context = tmp_path / "context"
    context.mkdir()
    containerfile = context / "Containerfile"
    containerfile.write_text("FROM scratch\n")
    request = WorkloadBuildRequest(
        engine="podman",
        context=context,
        containerfile=containerfile,
        repository="registry.example.test/acme/custom",
        tag="0.1.0",
        platform="linux/amd64",
    )
    monkeypatch.setattr(
        "syntara_plugin.sdk.workload.subprocess.run",
        lambda *args, **kwargs: pytest.fail("Podman ran"),
    )
    _assert_workload_build_rejected(request, "compiled descriptor")

    invalid = _descriptor()
    invalid["spec"]["plugin_revision_id"] = "sha256:" + "0" * 64
    _assert_workload_build_rejected(
        replace(request, registration_descriptor=invalid, workload_id="plugin-workload"),
        "plugin revision ID",
    )
    stale_schema = _descriptor()
    target = stale_schema["spec"]["targets"][0]
    target["input"] = {"type": "string"}
    target["step_revision_id"] = step_revision_id(stale_schema, target)
    stale_schema["spec"]["plugin_revision_id"] = plugin_revision_id(stale_schema)
    _assert_workload_build_rejected(
        replace(request, registration_descriptor=stale_schema, workload_id="plugin-workload"),
        "schema digest does not match",
    )


def test_loopback_workload_push_requires_explicit_opt_in_and_passes_tls_flag(
    tmp_path: Path, monkeypatch
) -> None:
    """Local HTTP is opt-in and cannot weaken a non-loopback workload push."""
    context = tmp_path / "context"
    context.mkdir()
    containerfile = context / "Containerfile"
    containerfile.write_text("FROM scratch\n", encoding="utf-8")
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "push":
            Path(command[3]).write_text("sha256:" + "d" * 64, encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("syntara_plugin.sdk.workload.subprocess.run", run)
    image = build_and_push_workload(
        WorkloadBuildRequest(
            engine="podman",
            context=context,
            containerfile=containerfile,
            repository="localhost:8443/syntara-admin/custom",
            tag="0.1.0",
            platform="linux/arm64",
            allow_insecure_loopback_http=True,
            registration_descriptor=_descriptor(),
            workload_id="plugin-workload",
        )
    )

    assert image == "localhost:8443/syntara-admin/custom@sha256:" + "d" * 64
    assert commands[2][4:] == [
        "--tls-verify=false",
        "localhost:8443/syntara-admin/custom:0.1.0",
    ]
