"""Container-engine detection and lifecycle for managed image-mode runs.

When ``syntara-plugin run --mode image`` is invoked without ``--address`` or
TLS options, the CLI manages the workload container itself: it detects Podman
or Docker, verifies the workspace's declared workload image exists locally,
runs it, waits for its gRPC health endpoint, and removes it afterwards.

The container publishes an ephemeral loopback host port secured with mutual
TLS generated for this invocation (one throwaway CA plus server and client
certificates, removed afterwards), which works everywhere published ports do,
including Docker Desktop. The managed path requires openssl and never falls
back to an unauthenticated listener: a fixed-port cleartext runtime would let
any local process impersonate the container and capture inputs. The author
can always opt out by running the runtime themselves and passing
``--address`` (and ``--tls-*``) explicitly.
A container whose process exits before its runtime becomes healthy is
reported fast: the health retry loop consults a liveness probe that reads
the container's exit status and its last log lines, so the author sees why
the workload died instead of a generic timeout.
"""

from __future__ import annotations

import json
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from syntara_plugin.runtime.abi import CONTAINER_ABI

from .runner import RunExecutionError, RunTarget, TlsOptions

_ENGINE_COMMANDS = ("podman", "docker")
_SELF_MANAGED_NEXT_STEP = "or start the runtime yourself and pass --address HOST:PORT"
_VERSION_TIMEOUT_SECONDS = 10
_INSPECT_TIMEOUT_SECONDS = 30
_RUN_TIMEOUT_SECONDS = 60
_OPENSSL_TIMEOUT_SECONDS = 30
_LOG_TAIL_LINES = 20
_LOG_LINE_LENGTH = 400
_LIVENESS_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class ContainerEngine:
    """One usable local container engine."""

    command: str


@dataclass(frozen=True)
class ManagedRuntime:
    """One container-managed workload runtime owned by this invocation."""

    engine: ContainerEngine
    image: str
    container_name: str
    address: str
    tls: TlsOptions | None = None
    liveness: Callable[[], None] | None = None


def detect_engine() -> ContainerEngine | None:
    """Return the first working engine, preferring Podman over Docker."""

    engines = detect_engines()
    return engines[0] if engines else None


def detect_engines() -> list[ContainerEngine]:
    """Return every installed engine in preference order (Podman first).

    ``--version`` only proves the client binary runs; a stopped machine or
    daemon, or an image living in the other engine's storage, is handled by
    :func:`_select_engine` trying the next candidate.
    """

    engines: list[ContainerEngine] = []
    for command in _ENGINE_COMMANDS:
        if shutil.which(command) is None:
            continue
        try:
            completed = subprocess.run(
                [command, "--version"],
                capture_output=True,
                text=True,
                timeout=_VERSION_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            engines.append(ContainerEngine(command=command))
    return engines


def _select_engine(image: str) -> ContainerEngine:
    """Return the first engine that is usable and holds the image.

    An engine whose daemon is unreachable, or whose storage lacks the image,
    is skipped in favor of the next installed one; when none works, one error
    lists what happened to each candidate.
    """

    engines = detect_engines()
    if not engines:
        raise RunExecutionError(
            "neither podman nor docker is available to run the workload image",
            next_steps=(
                "install podman or docker",
                _SELF_MANAGED_NEXT_STEP,
            ),
        )
    failures: list[str] = []
    for engine in engines:
        try:
            if _image_available(engine, image):
                return engine
            failures.append(f"{engine.command}: the image is not in its local storage")
        except RunExecutionError as error:
            failures.append(f"{engine.command}: {error.message}")
    raise RunExecutionError(
        f"no usable container engine has the workload image {image!r} locally",
        detail=tuple(failures),
        next_steps=(
            f"build and tag the image as {image!r} (see the image-mode guide)",
            _SELF_MANAGED_NEXT_STEP,
        ),
    )


@contextmanager
def managed_runtime(image: str, target: RunTarget | None = None) -> Iterator[ManagedRuntime]:
    """Run one workload image for the duration of the block, then remove it.

    Raises :class:`RunExecutionError` with author-facing guidance when no
    engine is usable, the image is not available locally, the local TLS
    material cannot be generated, or the container cannot start. Images are
    never pulled: a missing image is an error, not a side effect. There is
    deliberately no unauthenticated fallback: a fixed-port cleartext listener
    would let any local process impersonate the runtime and capture inputs.

    The container is started without ``--rm`` and removed only after the
    block, so a container that exits early keeps its exit status and log
    readable for the liveness probe and for post-mortem inspection.
    """

    engine = _select_engine(image)
    name = f"syntara-run-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="syntara-run-tls-") as temporary:
        material = _generate_tls_material(Path(temporary))
        if material is None:
            raise RunExecutionError(
                "openssl is required to secure the managed workload container",
                next_steps=(
                    "install openssl",
                    _SELF_MANAGED_NEXT_STEP,
                ),
            )
        # SIGTERM (timeout, CI cancellation, IDE stop) must unwind through
        # the finally like Ctrl-C does; restore the previous handler after.
        previous_handler = _install_sigterm_exit()
        # The cleanup guard starts before the container is created, not after:
        # a start failure (a port race, the engine timeout, Ctrl-C) must not
        # leave the named container behind. Removal is best effort, so a
        # failure before creation is harmless.
        try:
            resolved_path: Path | None = None
            if target is not None:
                resolved_path = Path(temporary) / "resolved.json"
                try:
                    resolved_path.write_text(
                        json.dumps({"bindings": [{
                            "stepRevisionId": target.step_revision_id,
                            "stepIdentity": target.identity,
                            "runtimeKind": "custom-runner",
                            "entryPoint": target.entry_point,
                            "abi": CONTAINER_ABI,
                            "schemaDigests": target.schema_digests,
                        }]}),
                        encoding="utf-8",
                    )
                    resolved_path.chmod(0o644)
                except OSError as error:
                    raise RunExecutionError(
                        "could not prepare resolved execution data for the managed workload container",
                        next_steps=(
                            "check the permissions and available space in the temporary directory",
                            _SELF_MANAGED_NEXT_STEP,
                        ),
                    ) from error
            runtime = replace(
                _published_runtime(engine, image, name, material, resolved_path)
                if resolved_path is not None
                else _published_runtime(engine, image, name, material),
                liveness=_container_liveness(engine, name, image),
            )
            yield runtime
        finally:
            _restore_sigterm(previous_handler)
            _remove_container(engine, name)


def _published_runtime(
    engine: ContainerEngine,
    image: str,
    name: str,
    material: _TlsMaterial,
    resolved_path: Path | None = None,
) -> ManagedRuntime:
    """Run the container on a published loopback port with generated mTLS."""

    port = _free_port()
    # Both engines support the SELinux relabel; without it, Docker on an
    # SELinux-enforcing host cannot read the mounted material (it stays
    # labeled user_tmp_t) and the runtime dies with EACCES. Where SELinux is
    # off the flag is a no-op.
    mount_mode = "ro,Z"
    try:
        completed = subprocess.run(
            [
                engine.command,
                "run",
                # No --rm: the finally-block removal keeps an exited
                # container's status and log readable for the liveness probe.
                "--detach",
                "--name",
                name,
                # Never resolve the image from a registry: the local check above
                # is the authority, and a pull would run unrequested content.
                "--pull=never",
                "--publish",
                f"127.0.0.1:{port}:50051",
                "--volume",
                f"{material.server_certificate}:/run/secrets/server.crt:{mount_mode}",
                "--volume",
                f"{material.server_key}:/run/secrets/server.key:{mount_mode}",
                "--volume",
                f"{material.client_ca}:/run/secrets/client-ca.crt:{mount_mode}",
                "--env",
                "SYNTARA_RUNTIME_GRPC_ADDRESS=0.0.0.0:50051",
                "--env",
                "SYNTARA_RUNTIME_GRPC_TLS_CERT=/run/secrets/server.crt",
                "--env",
                "SYNTARA_RUNTIME_GRPC_TLS_KEY=/run/secrets/server.key",
                "--env",
                "SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA=/run/secrets/client-ca.crt",
                *(
                    [
                        "--volume",
                        f"{resolved_path}:/run/syntara/resolved.json:{mount_mode}",
                        "--env",
                        "SYNTARA_RESOLVED_EXECUTION=/run/syntara/resolved.json",
                    ]
                    if resolved_path is not None
                    else []
                ),
                image,
            ],
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RunExecutionError(
            f"the workload container {image!r} did not start "
            f"within {_RUN_TIMEOUT_SECONDS} seconds",
            next_steps=("check the engine and the image, then rerun",),
        ) from error
    if completed.returncode != 0:
        raise RunExecutionError(
            f"the workload container {image!r} could not start "
            f"({engine.command} exit {completed.returncode})",
            detail=_stderr_tail(completed.stderr),
            next_steps=(
                "check the engine error above",
                _SELF_MANAGED_NEXT_STEP,
            ),
        )
    return ManagedRuntime(
        engine=engine,
        image=image,
        container_name=name,
        address=f"127.0.0.1:{port}",
        tls=TlsOptions(
            ca=material.client_ca,
            certificate=material.client_certificate,
            key=material.client_key,
        ),
    )


def _install_sigterm_exit() -> Any:
    """Turn SIGTERM into an exception so cleanup paths run; return the old handler."""

    def _terminate(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    try:
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _terminate)
        return previous
    except ValueError:
        # Not the main thread; the process-level default applies.
        return None


def _restore_sigterm(previous: Any) -> None:
    """Restore the SIGTERM handler captured by _install_sigterm_exit."""

    if previous is None:
        return
    try:
        signal.signal(signal.SIGTERM, previous)
    except ValueError:
        pass


def _container_liveness(engine: ContainerEngine, name: str, image: str) -> Callable[[], None]:
    """Return a probe that raises once the managed container has exited.

    The probe is consulted by the runner's health retry loop, so a container
    whose process died fails fast with its exit status and log instead of
    consuming the whole startup window. Checks are rate-limited to once per
    second because the loop retries every quarter second.
    """

    last_check = 0.0

    def probe() -> None:
        nonlocal last_check
        now = time.monotonic()
        if now - last_check < _LIVENESS_INTERVAL_SECONDS:
            return
        last_check = now
        exit_code = _container_exit_code(engine, name)
        if exit_code is not None:
            raise _container_exit_error(engine, name, image, exit_code)

    return probe


def _container_exit_code(engine: ContainerEngine, name: str) -> int | None:
    """Return the container's exit status, or None while it is still running."""

    try:
        completed = subprocess.run(
            [
                engine.command,
                "inspect",
                "--format",
                "{{.State.Running}} {{.State.ExitCode}}",
                name,
            ],
            capture_output=True,
            text=True,
            timeout=_INSPECT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    running, _, code = completed.stdout.partition(" ")
    if running.strip().lower() == "true":
        return None
    try:
        return int(code.strip())
    except ValueError:
        return None


def _container_exit_error(
    engine: ContainerEngine, name: str, image: str, exit_code: int
) -> RunExecutionError:
    """Build the failure for a managed container that exited before health."""

    detail = [f"container {name} exited with status {exit_code}"]
    logs = _container_log_tail(engine, name)
    if logs:
        detail.append("container log (last lines):")
        detail.extend(logs)
    return RunExecutionError(
        f"the workload container for {image!r} exited with status {exit_code} "
        "before its runtime became healthy",
        detail=tuple(detail),
        next_steps=(
            "check the container log above",
            "or rerun with --mode source to execute the Step from the workspace",
        ),
    )


def _container_log_tail(
    engine: ContainerEngine, name: str, *, limit: int = _LOG_TAIL_LINES
) -> tuple[str, ...]:
    """Return the last lines of the container's combined output, bounded."""

    try:
        completed = subprocess.run(
            [engine.command, "logs", "--tail", str(limit), name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=_INSPECT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()
    return tuple(
        line[:_LOG_LINE_LENGTH] for line in completed.stdout.splitlines()[-limit:]
    )


def _remove_container(engine: ContainerEngine, name: str) -> None:
    """Remove the managed container; best effort so cleanup never masks a failure.

    ``rm --force`` on a container that never existed exits nonzero by design,
    so a failed removal only warns when the container is still present
    afterwards; the author then gets the exact command to clean it up.
    """

    try:
        completed = subprocess.run(
            [engine.command, "rm", "--force", name],
            capture_output=True,
            timeout=_RUN_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if (completed is None or completed.returncode != 0) and _container_exists(engine, name):
        print(
            f"warning: could not remove the managed container {name}; "
            f"remove it with `{engine.command} rm -f {name}`",
            file=sys.stderr,
        )


def _container_exists(engine: ContainerEngine, name: str) -> bool:
    """Return whether the named container is still present."""

    try:
        completed = subprocess.run(
            [engine.command, "inspect", "--format", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=_INSPECT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return completed.returncode == 0


@dataclass(frozen=True)
class _TlsMaterial:
    """One ephemeral CA with server and client certificates."""

    client_ca: Path
    client_certificate: Path
    client_key: Path
    server_certificate: Path
    server_key: Path


def _generate_tls_material(directory: Path) -> _TlsMaterial | None:
    """Generate one ephemeral CA plus server and client certificates.

    The certificates live only for this invocation and are removed with their
    temporary directory afterwards. Returns ``None`` when openssl is not
    installed; any generation failure raises with the underlying stderr.
    """

    if shutil.which("openssl") is None:
        return None
    ca_key = directory / "ca.key"
    ca_certificate = directory / "ca.crt"
    server_key = directory / "server.key"
    server_certificate = directory / "server.crt"
    client_key = directory / "client.key"
    client_certificate = directory / "client.crt"
    extensions = directory / "server.ext"
    extensions.write_text(
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n", encoding="utf-8"
    )
    _run_openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca_certificate),
        "-days",
        "1",
        "-subj",
        "/CN=syntara-run-ca",
    )
    for role, key, certificate in (
        ("server", server_key, server_certificate),
        ("client", client_key, client_certificate),
    ):
        request = directory / f"{role}.csr"
        _run_openssl(
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(request),
            "-subj",
            f"/CN=syntara-run-{role}",
        )
        command = [
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
            "-sha256",
        ]
        if role == "server":
            command.extend(("-extfile", str(extensions)))
        _run_openssl(*command)
    # The container's runtime user must be able to read the mounted material.
    for path in (
        ca_certificate,
        server_certificate,
        server_key,
        client_certificate,
        client_key,
    ):
        path.chmod(0o644)
    return _TlsMaterial(
        client_ca=ca_certificate,
        client_certificate=client_certificate,
        client_key=client_key,
        server_certificate=server_certificate,
        server_key=server_key,
    )


def _run_openssl(*arguments: str) -> None:
    try:
        completed = subprocess.run(
            ("openssl", *arguments),
            capture_output=True,
            text=True,
            check=False,
            timeout=_OPENSSL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise RunExecutionError(
            "the local TLS material for the managed container could not be generated",
            detail=(f"openssl did not finish within {_OPENSSL_TIMEOUT_SECONDS} seconds",),
            next_steps=("check the openssl error above and rerun",),
        ) from error
    if completed.returncode != 0:
        raise RunExecutionError(
            "the local TLS material for the managed container could not be generated",
            detail=_stderr_tail(completed.stderr),
            next_steps=("check the openssl error above and rerun",),
        )


def _free_port() -> int:
    """Reserve one free loopback port for the published container listener."""

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _image_available(engine: ContainerEngine, image: str) -> bool:
    if engine.command == "podman":
        command = [engine.command, "image", "exists", image]
    else:
        command = [engine.command, "image", "inspect", image]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_INSPECT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RunExecutionError(
            f"{engine.command} is installed but not usable",
            detail=(str(error),),
            next_steps=("start the engine and rerun", "or pass --address HOST:PORT"),
        ) from error
    if completed.returncode == 0:
        return True
    stderr = completed.stderr.lower()
    if engine.command == "docker":
        # Docker's ordinary missing-image response also mentions the daemon
        # ("Error response from daemon: No such image: ..."), so that case
        # must be classified as a missing image before any engine check.
        if "no such image" in stderr:
            return False
        unusable = (
            "cannot connect" in stderr
            or "daemon is unreachable" in stderr
            or "permission denied" in stderr
        )
    else:
        # Podman flags an infrastructure failure (no machine started, a dead
        # PODMAN_HOST) with its infra-failure exit status 125 or a connection
        # complaint on stderr. The image name is schema-constrained to
        # ^[a-z][a-z0-9-]{0,62}$ (a logical image ID), so exit 125 cannot be
        # a malformed-reference case here.
        unusable = (
            completed.returncode == 125
            or "cannot connect" in stderr
            or "unable to connect" in stderr
        )
    if unusable:
        raise RunExecutionError(
            f"{engine.command} is installed but not usable",
            detail=_stderr_tail(completed.stderr),
            next_steps=("start the engine and rerun", "or pass --address HOST:PORT"),
        )
    return False


def _stderr_tail(stderr: str, *, limit: int = 400) -> tuple[str, ...]:
    text = stderr.strip()
    if not text:
        return ()
    return tuple(text.splitlines()[-limit // 40 :])
