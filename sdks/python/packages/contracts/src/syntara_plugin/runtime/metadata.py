"""Probe the runtime components declared for one immutable workload image."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

_PROBE_TIMEOUT_SECONDS = 5
_MAX_PROBE_OUTPUT_BYTES = 4096
_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+()_-]{0,255}$")


class RuntimeComponentProbeError(ValueError):
    """Raised when a supported runtime probe fails or returns unsafe data."""


def probe_runtime_components(
    declarations: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    """Return observed metadata for declared executables in the current image.

    The probe never invokes a shell. It uses a small allowlist of version
    commands and a minimal environment so secrets in the server environment
    are not copied into a child process. Unknown component names remain
    explicitly unverified instead of guessing a version convention.
    """

    observations: list[dict[str, str]] = []
    for declaration in declarations:
        name = declaration.get("name")
        executable = declaration.get("executable")
        if not isinstance(name, str) or not isinstance(executable, str):
            raise RuntimeComponentProbeError("runtime component declaration is invalid")
        if name.casefold() == "python" and os.path.realpath(sys.executable) != os.path.realpath(
            executable
        ):
            raise RuntimeComponentProbeError(
                "the running Python interpreter differs from the declared executable"
            )
        observation = {"name": name, "executable": executable, "verification": "unverified"}
        probe = _probe_for_name(name)
        if probe is not None:
            version = probe(executable)
            if not _VERSION_PATTERN.fullmatch(version):
                raise RuntimeComponentProbeError(
                    f"runtime probe for {name!r} returned an invalid version"
                )
            observation["version"] = version
            observation["verification"] = "verified"
        observations.append(observation)
    return tuple(observations)


def _probe_for_name(name: str) -> Callable[[str], str] | None:
    normalized = name.casefold()
    if normalized == "python":
        return _probe_python
    if normalized == "bash":
        return _probe_bash
    return None


def _run_probe(executable: str, arguments: Sequence[str]) -> str:
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    try:
        completed = subprocess.run(
            [executable, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env=environment,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeComponentProbeError("a declared runtime executable could not be probed") from error
    if completed.returncode != 0 or len(completed.stdout) > _MAX_PROBE_OUTPUT_BYTES:
        raise RuntimeComponentProbeError("a declared runtime executable probe failed")
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RuntimeComponentProbeError("a runtime probe returned non-text output") from error


def _probe_python(executable: str) -> str:
    output = _run_probe(
        executable,
        (
            "-c",
            "import json,sys; print(json.dumps({'version': '.'.join(map(str, sys.version_info[:3])), 'executable': sys.executable}))",
        ),
    )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeComponentProbeError("the Python runtime probe returned invalid data") from error
    version = value.get("version") if isinstance(value, Mapping) else None
    observed_executable = value.get("executable") if isinstance(value, Mapping) else None
    if not isinstance(version, str) or not isinstance(observed_executable, str):
        raise RuntimeComponentProbeError("the Python runtime probe returned incomplete data")
    if os.path.realpath(observed_executable) != os.path.realpath(executable):
        raise RuntimeComponentProbeError(
            "the Python runtime used by the workload differs from the declared executable"
        )
    return version


def _probe_bash(executable: str) -> str:
    output = _run_probe(executable, ("--version",))
    match = re.search(r"GNU bash, version ([0-9]+\.[0-9]+(?:\.[0-9]+)?)", output)
    if match is None:
        raise RuntimeComponentProbeError("the declared Bash executable is not GNU Bash")
    return match.group(1)
