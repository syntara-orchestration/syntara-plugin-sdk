"""Unit checks for the managed image-mode container engine."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from syntara_plugin.sdk import container_engine
from syntara_plugin.sdk.container_engine import (
    _free_port,
    _generate_tls_material,
    detect_engine,
)
from syntara_plugin.sdk.runner import RunExecutionError


def test_detect_engine_returns_a_usable_engine() -> None:
    """Podman is preferred; docker is the fallback; neither yields None."""

    engine = detect_engine()
    if shutil.which("podman") is None and shutil.which("docker") is None:
        assert engine is None
        return
    assert engine is not None
    assert engine.command in {"podman", "docker"}


def test_free_port_returns_a_bindable_loopback_port() -> None:
    port = _free_port()

    assert 1 <= port <= 65_535
    with __import__("socket").socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_generated_tls_material_is_complete_and_readable(tmp_path: Path) -> None:
    """The ephemeral CA and certificates exist and the container user can read them."""

    if shutil.which("openssl") is None:
        pytest.skip("openssl is required for TLS generation")
    material = _generate_tls_material(tmp_path)

    assert material is not None
    for path in (
        material.client_ca,
        material.client_certificate,
        material.client_key,
        material.server_certificate,
        material.server_key,
    ):
        assert path.is_file()
        assert path.stat().st_mode & 0o044


def test_generate_tls_material_returns_none_without_openssl(
    tmp_path: Path, monkeypatch
) -> None:
    """A missing openssl means no TLS; the managed path requires it."""

    monkeypatch.setattr(
        "syntara_plugin.sdk.container_engine.shutil.which", lambda name: None
    )

    assert _generate_tls_material(tmp_path) is None


def test_a_missing_image_fails_at_the_local_check_without_any_side_effect(
    monkeypatch,
) -> None:
    """A missing image is refused before any run, pull, or build is attempted."""

    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        if list(command[1:]) == ["image", "exists", "missing-image"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        raise AssertionError(f"unexpected engine command: {command}")

    monkeypatch.setattr(container_engine.subprocess, "run", fake_run)
    monkeypatch.setattr(
        container_engine, "detect_engine", lambda: container_engine.ContainerEngine("podman")
    )

    with pytest.raises(RunExecutionError) as raised:
        with container_engine.managed_runtime("missing-image"):
            pass

    assert "is not available locally" in raised.value.message
    # The local existence check is the only engine command ever issued: the
    # managed path never builds, pulls, or starts a missing image.
    assert commands == [["podman", "image", "exists", "missing-image"]]


def test_managed_runtime_requires_openssl_instead_of_falling_back(
    monkeypatch,
) -> None:
    """Without openssl the managed path errors; there is no cleartext fallback."""

    monkeypatch.setattr(
        container_engine, "_generate_tls_material", lambda directory: None
    )
    monkeypatch.setattr(
        container_engine, "detect_engine", lambda: container_engine.ContainerEngine("podman")
    )

    def fake_run(command, **_kwargs):
        if list(command[1:]) == ["image", "exists", "some-image"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected engine command: {command}")

    monkeypatch.setattr(container_engine.subprocess, "run", fake_run)

    with pytest.raises(RunExecutionError) as raised:
        with container_engine.managed_runtime("some-image"):
            pass

    assert "openssl is required" in raised.value.message


def test_published_runtime_never_pulls_and_never_uses_the_host_network(
    monkeypatch, tmp_path
) -> None:
    """The managed run command pins the pull policy and avoids host networking."""

    from syntara_plugin.sdk.container_engine import _published_runtime, _TlsMaterial

    material = _TlsMaterial(
        client_ca=tmp_path / "ca.crt",
        client_certificate=tmp_path / "client.crt",
        client_key=tmp_path / "client.key",
        server_certificate=tmp_path / "server.crt",
        server_key=tmp_path / "server.key",
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        return SimpleNamespace(returncode=0, stdout="container-id", stderr="")

    monkeypatch.setattr(container_engine.subprocess, "run", fake_run)

    runtime = _published_runtime(
        container_engine.ContainerEngine("podman"), "some-image", "a-name", material
    )

    assert runtime.address.startswith("127.0.0.1:")
    assert runtime.tls is not None
    run_command = next(command for command in commands if command[1] == "run")
    assert "--pull=never" in run_command
    assert "--network" not in run_command
    # No --rm: the container must survive its own exit so the liveness probe
    # can read its status and log, and the finally-block removal stays the
    # single cleanup path.
    assert "--rm" not in run_command


def test_liveness_probe_raises_with_exit_status_and_logs(monkeypatch) -> None:
    """A dead container fails fast with its exit code and log tail."""

    def fake_run(command, **_kwargs):
        arguments = list(command)
        if "inspect" in arguments:
            return SimpleNamespace(returncode=0, stdout="false 1\n", stderr="")
        if "logs" in arguments:
            return SimpleNamespace(returncode=0, stdout="workload startup failed\n", stderr="")
        raise AssertionError(f"unexpected engine command: {command}")

    monkeypatch.setattr(container_engine.subprocess, "run", fake_run)
    engine = container_engine.ContainerEngine("podman")

    probe = container_engine._container_liveness(engine, "syntara-run-x", "some-image")

    with pytest.raises(RunExecutionError) as raised:
        probe()

    assert "exited with status 1" in raised.value.message
    assert any("workload startup failed" in line for line in raised.value.detail)
    assert any("--mode source" in step for step in raised.value.next_steps)


def test_liveness_probe_stays_quiet_while_the_container_runs(monkeypatch) -> None:
    """A running container never trips the probe."""

    def fake_run(command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="true 0\n", stderr="")

    monkeypatch.setattr(container_engine.subprocess, "run", fake_run)
    engine = container_engine.ContainerEngine("podman")

    probe = container_engine._container_liveness(engine, "syntara-run-x", "some-image")

    probe()


def test_managed_runtime_attaches_the_liveness_probe(monkeypatch, tmp_path) -> None:
    """The managed runtime carries a probe wired to its own container."""

    from syntara_plugin.sdk.container_engine import _TlsMaterial

    material = _TlsMaterial(
        client_ca=tmp_path / "ca.crt",
        client_certificate=tmp_path / "client.crt",
        client_key=tmp_path / "client.key",
        server_certificate=tmp_path / "server.crt",
        server_key=tmp_path / "server.key",
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        return SimpleNamespace(returncode=0, stdout="true 0\n", stderr="")

    monkeypatch.setattr(container_engine.subprocess, "run", fake_run)
    monkeypatch.setattr(
        container_engine, "detect_engine", lambda: container_engine.ContainerEngine("podman")
    )
    monkeypatch.setattr(
        container_engine, "_image_available", lambda engine, image: True
    )
    monkeypatch.setattr(
        container_engine, "_generate_tls_material", lambda directory: material
    )

    with container_engine.managed_runtime("some-image") as runtime:
        assert runtime.liveness is not None
        runtime.liveness()
        run_command = next(command for command in commands if command[1] == "run")
        name = run_command[run_command.index("--name") + 1]
        assert name.startswith("syntara-run-")

    assert commands[-1][:3] == ["podman", "rm", "--force"]
