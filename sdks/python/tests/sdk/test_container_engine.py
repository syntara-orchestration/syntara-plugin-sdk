"""Unit checks for the managed image-mode container engine."""

from __future__ import annotations

import json
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
from syntara_plugin.sdk.runner import RunExecutionError, RunTarget


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
        container_engine, "detect_engines", lambda: [container_engine.ContainerEngine("podman")]
    )

    with pytest.raises(RunExecutionError) as raised:
        with container_engine.managed_runtime("missing-image"):
            pass

    assert "no usable container engine" in raised.value.message
    # The local existence check is the only engine command ever issued: the
    # managed path never builds, pulls, or starts a missing image.
    assert commands == [["podman", "image", "exists", "missing-image"]]


def test_a_down_podman_is_not_reported_as_a_missing_image(monkeypatch) -> None:
    """A podman connection failure is an unusable engine, not a missing image."""

    def fake_run(command, **_kwargs):
        if list(command[1:]) == ["image", "exists", "some-image"]:
            return SimpleNamespace(
                returncode=125,
                stdout="",
                stderr="Error: unable to connect to Podman socket: "
                "unix:///run/user/1000/podman/podman.sock: no such file or directory\n",
            )
        raise AssertionError(f"unexpected engine command: {command}")

    monkeypatch.setattr(container_engine.subprocess, "run", fake_run)
    engine = container_engine.ContainerEngine("podman")

    with pytest.raises(RunExecutionError) as raised:
        container_engine._image_available(engine, "some-image")

    assert "podman is installed but not usable" in raised.value.message
    assert any("unable to connect" in line for line in raised.value.detail)
    assert any("--address" in step for step in raised.value.next_steps)


def test_managed_runtime_requires_openssl_instead_of_falling_back(
    monkeypatch,
) -> None:
    """Without openssl the managed path errors; there is no cleartext fallback."""

    monkeypatch.setattr(
        container_engine, "_generate_tls_material", lambda directory: None
    )
    monkeypatch.setattr(
        container_engine, "detect_engines", lambda: [container_engine.ContainerEngine("podman")]
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


def test_managed_runtime_mounts_selected_resolved_revision(monkeypatch, tmp_path: Path) -> None:
    from syntara_plugin.sdk.container_engine import _TlsMaterial

    material = _TlsMaterial(
        client_ca=tmp_path / "ca.crt", client_certificate=tmp_path / "client.crt",
        client_key=tmp_path / "client.key", server_certificate=tmp_path / "server.crt",
        server_key=tmp_path / "server.key",
    )
    revision = "sha256:" + "a" * 64
    target = RunTarget("identity", "Action", "example:Echo", "echo", {}, "image", revision)
    mounted: list[Path] = []

    def fake_run(command, **_kwargs):
        if command[1] == "run":
            volume = next(value for value in command if ":/run/syntara/resolved.json:" in value)
            path = Path(volume.split(":", 1)[0])
            mounted.append(path)
            assert json.loads(path.read_text(encoding="utf-8")) == {"bindings": [{
                "stepRevisionId": revision, "runtimeKind": "custom-runner",
                "entryPoint": "example:Echo", "abi": "syntara.container/v1alpha1",
            }]}
            assert volume.endswith(":ro,Z")
            assert "SYNTARA_RESOLVED_EXECUTION=/run/syntara/resolved.json" in command
        return SimpleNamespace(returncode=0, stdout="true 0\n", stderr="")

    monkeypatch.setattr(container_engine.subprocess, "run", fake_run)
    monkeypatch.setattr(container_engine, "_select_engine", lambda image: container_engine.ContainerEngine("podman"))
    monkeypatch.setattr(container_engine, "_generate_tls_material", lambda directory: material)
    with container_engine.managed_runtime("image", target):
        assert mounted[0].exists()
    assert not mounted[0].exists()


@pytest.mark.parametrize("method", ["write_text", "chmod"])
def test_resolved_execution_file_failure_is_safe(monkeypatch, method: str) -> None:
    target = RunTarget("identity", "Action", "example:Echo", "echo", {}, "image", "revision")
    removed: list[str] = []
    original = getattr(Path, method)

    def fail_resolved(path, *args, **kwargs):
        if path.name == "resolved.json":
            raise OSError("private filesystem detail")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, fail_resolved)
    monkeypatch.setattr(container_engine, "_select_engine", lambda image: container_engine.ContainerEngine("podman"))
    monkeypatch.setattr(container_engine, "_generate_tls_material", lambda directory: object())
    monkeypatch.setattr(container_engine, "_remove_container", lambda engine, name: removed.append(name))
    monkeypatch.setattr(
        container_engine, "_published_runtime",
        lambda *args: pytest.fail("container should not start"),
    )

    with pytest.raises(RunExecutionError) as raised:
        with container_engine.managed_runtime("image", target):
            pytest.fail("runtime should not start")

    assert "resolved execution data" in raised.value.message
    assert "private filesystem detail" not in str(raised.value)
    assert len(removed) == 1


def test_a_startup_failure_still_removes_the_container(monkeypatch) -> None:
    """A failure between container creation and yield must not leak the container."""

    removed: list[str] = []
    monkeypatch.setattr(
        container_engine,
        "_generate_tls_material",
        lambda directory: SimpleNamespace(
            client_ca=None, client_certificate=None, client_key=None,
            server_certificate=None, server_key=None,
        ),
    )
    monkeypatch.setattr(
        container_engine, "detect_engines", lambda: [container_engine.ContainerEngine("podman")]
    )
    monkeypatch.setattr(container_engine, "_image_available", lambda engine, image: True)
    monkeypatch.setattr(
        container_engine.subprocess,
        "run",
        lambda command, **_kwargs: SimpleNamespace(returncode=0, stdout="id", stderr=""),
    )
    monkeypatch.setattr(
        container_engine,
        "_container_liveness",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("probe construction failed")),
    )
    monkeypatch.setattr(
        container_engine, "_remove_container", lambda engine, name: removed.append(name)
    )

    with pytest.raises(RuntimeError):
        with container_engine.managed_runtime("some-image"):
            pass

    assert len(removed) == 1


def test_docker_missing_image_is_not_reported_as_an_unusable_engine(
    monkeypatch,
) -> None:
    """Docker's ordinary missing-image response mentions the daemon; classify it."""

    engine = container_engine.ContainerEngine("docker")

    def fake_run(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Error response from daemon: No such image: missing-image:latest",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(container_engine.subprocess, "run", fake_run)

    assert container_engine._image_available(engine, "missing-image") is False


def test_docker_connection_failure_is_reported_as_an_unusable_engine(
    monkeypatch,
) -> None:
    engine = container_engine.ContainerEngine("docker")

    def fake_run(command, **_kwargs):
        if command[1:3] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(container_engine.subprocess, "run", fake_run)

    with pytest.raises(RunExecutionError) as raised:
        container_engine._image_available(engine, "any-image")

    assert "not usable" in raised.value.message


def test_mounted_tls_material_is_relabelled_for_both_engines(monkeypatch, tmp_path) -> None:
    """Both podman and docker mounts carry the SELinux relabel flag."""

    material = container_engine._TlsMaterial(
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

    for command_name in ("podman", "docker"):
        commands.clear()
        container_engine._published_runtime(
            container_engine.ContainerEngine(command_name), "some-image", "a-name", material
        )
        volumes = [part for part in commands[0] if ":/run/secrets/" in str(part)]
        assert volumes, command_name
        for volume in volumes:
            assert volume.endswith(":ro,Z"), (command_name, volume)


def test_engine_fallback_tries_docker_when_podman_is_unusable(monkeypatch) -> None:
    """A stopped podman machine falls back to a working docker daemon."""

    from syntara_plugin.sdk import container_engine as ce

    def fake_run(command, **_kwargs):
        command = list(command)
        if command == ["podman", "image", "exists", "an-image"]:
            return SimpleNamespace(
                returncode=125, stdout="", stderr="Error: unable to connect to Podman socket"
            )
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        if command[:2] == ["docker", "run"]:
            return SimpleNamespace(returncode=0, stdout="container-id", stderr="")
        if command[:2] == ["docker", "rm"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="no such object")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(
        ce, "detect_engines", lambda: [ce.ContainerEngine("podman"), ce.ContainerEngine("docker")]
    )
    monkeypatch.setattr(ce.subprocess, "run", fake_run)
    monkeypatch.setattr(ce, "_generate_tls_material", lambda directory: _fake_material())

    engine = ce._select_engine("an-image")
    assert engine.command == "docker"


def test_engine_failure_lists_every_candidate(monkeypatch) -> None:
    """When no engine works, one error says what happened to each."""

    from syntara_plugin.sdk import container_engine as ce

    monkeypatch.setattr(
        ce, "detect_engines", lambda: [ce.ContainerEngine("podman"), ce.ContainerEngine("docker")]
    )
    monkeypatch.setattr(
        ce.subprocess,
        "run",
        lambda command, **_k: SimpleNamespace(
            returncode=1, stdout="", stderr="no such image"
        ),
    )

    with pytest.raises(RunExecutionError) as raised:
        ce._select_engine("an-image")

    assert "no usable container engine" in raised.value.message
    assert "podman:" in raised.value.detail[0]
    assert "docker:" in raised.value.detail[1]


def test_docker_permission_denied_is_an_unusable_engine(monkeypatch) -> None:
    """A socket permission failure is classified as unusable, not missing."""

    from syntara_plugin.sdk import container_engine as ce

    monkeypatch.setattr(ce, "detect_engines", lambda: [ce.ContainerEngine("docker")])
    monkeypatch.setattr(
        ce.subprocess,
        "run",
        lambda command, **_k: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="permission denied while trying to connect to the docker API",
        ),
    )

    with pytest.raises(RunExecutionError) as raised:
        ce._select_engine("an-image")

    assert "docker:" in raised.value.detail[0]


def test_removal_failure_warns_only_when_the_container_remains(monkeypatch, capsys) -> None:
    """A genuine rm failure warns with the cleanup command; not-found stays silent."""

    from syntara_plugin.sdk import container_engine as ce

    engine = ce.ContainerEngine("podman")

    def fake_run(command, **_kwargs):
        command = list(command)
        if command[:3] == ["podman", "rm", "--force"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="in use")
        if command[:2] == ["podman", "inspect"]:
            # First probe (existence): still there.
            return SimpleNamespace(returncode=0, stdout="false\n", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(ce.subprocess, "run", fake_run)

    ce._remove_container(engine, "leftover")
    captured = capsys.readouterr()
    assert "warning: could not remove the managed container leftover" in captured.err
    assert "podman rm -f leftover" in captured.err

    # When the container is gone after the failed rm, no warning.
    def gone_run(command, **_kwargs):
        command = list(command)
        if command[:3] == ["podman", "rm", "--force"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="no such container")
        if command[:2] == ["podman", "inspect"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="no such object")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(ce.subprocess, "run", gone_run)
    ce._remove_container(engine, "gone")
    assert capsys.readouterr().err == ""


def _fake_material():
    from syntara_plugin.sdk.container_engine import _TlsMaterial

    return _TlsMaterial(
        client_ca=Path("/tmp/x-ca.crt"),
        client_certificate=Path("/tmp/x-client.crt"),
        client_key=Path("/tmp/x-client.key"),
        server_certificate=Path("/tmp/x-server.crt"),
        server_key=Path("/tmp/x-server.key"),
    )


def test_sigterm_runs_the_cleanup(monkeypatch) -> None:
    """SIGTERM inside the managed block unwinds through the removal."""

    import os
    import signal
    import threading
    import time

    from syntara_plugin.sdk import container_engine as ce

    removed: list[str] = []
    monkeypatch.setattr(ce, "detect_engines", lambda: [ce.ContainerEngine("podman")])
    monkeypatch.setattr(ce, "_image_available", lambda engine, image: True)
    monkeypatch.setattr(ce, "_generate_tls_material", lambda directory: _fake_material())
    monkeypatch.setattr(
        ce.subprocess,
        "run",
        lambda command, **_k: SimpleNamespace(returncode=0, stdout="id", stderr=""),
    )
    monkeypatch.setattr(
        ce, "_container_liveness", lambda *a, **k: (lambda: None)
    )
    monkeypatch.setattr(ce, "_remove_container", lambda engine, name: removed.append(name))
    managed = ce.managed_runtime("an-image")
    managed.__enter__()

    def send_sigterm() -> None:
        time.sleep(0.2)
        os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=send_sigterm, daemon=True)
    thread.start()
    try:
        with pytest.raises(SystemExit):
            time.sleep(2)  # the SIGTERM handler raises SystemExit in this thread
    finally:
        managed.__exit__(None, None, None)
    assert len(removed) == 1
