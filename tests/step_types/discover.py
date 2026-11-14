"""Discover the step types to test from a syntara-step-types checkout.

The plugin list comes from step-types' own Makefile variable ``SDK_PLUGINS``
(the list its ``sdk-plugin-*`` targets accept), so a work-in-progress
directory in step-types does not turn the SDK check red. An empty or invalid
list is an error: the check must never pass while testing nothing.

Usage::

    python tests/step_types/discover.py STEP_TYPES_DIR

Prints the plugins as a JSON list and, inside GitHub Actions, writes the
``plugins``, ``pin``, and ``pin_date`` step outputs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from render import Renderer

_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_PRINT_TARGET = "step-types-compat-print-sdk-plugins"


class DiscoveryError(Exception):
    pass


def sdk_plugins(step_types: Path, make: str) -> list[str]:
    """Return ``$(SDK_PLUGINS)`` from the step-types Makefile, validated."""
    # A temporary makefile that includes theirs and prints the variable. This
    # needs only features GNU Make has had forever: `make --eval` (the obvious
    # tool) is missing from GNU Make 3.81, which is still a default on macOS.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".mak", encoding="utf-8", delete=False
    ) as handle:
        handle.write(f"include Makefile\n{_PRINT_TARGET}: ; @echo $(SDK_PLUGINS)\n")
        script = handle.name
    try:
        result = subprocess.run(
            [
                make,
                "-s",
                "--no-print-directory",
                "-C",
                str(step_types),
                "-f",
                script,
                _PRINT_TARGET,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        Path(script).unlink(missing_ok=True)
    if result.returncode != 0:
        raise DiscoveryError(f"could not read SDK_PLUGINS from the Makefile: {result.stderr.strip()}")
    names = result.stdout.split()
    if not names:
        raise DiscoveryError(
            "expected at least one plugin in step-types' SDK_PLUGINS, found none; the step-types "
            "fixture may have been restructured"
        )
    invalid = [name for name in names if not _PLUGIN_NAME.fullmatch(name)]
    if invalid:
        raise DiscoveryError(f"invalid plugin name(s) in SDK_PLUGINS: {', '.join(invalid)}")
    missing = [name for name in names if not (step_types / name / "syntara-plugin.yaml").is_file()]
    if missing:
        raise DiscoveryError(
            f"SDK_PLUGINS lists plugin(s) without a syntara-plugin.yaml: {', '.join(missing)}"
        )
    return sorted(dict.fromkeys(names))


def git(step_types: Path, *args: str) -> str:
    executable = shutil.which("git")
    if not executable:
        raise DiscoveryError("git was not found on PATH")
    return subprocess.run(
        [executable, "-C", str(step_types), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    out = Renderer()
    if len(arguments) != 1:
        print("usage: discover.py STEP_TYPES_DIR", file=sys.stderr)
        return 2
    step_types = Path(arguments[0]).resolve()
    make = shutil.which("make")
    try:
        if not make:
            raise DiscoveryError("make was not found on PATH")
        plugins = sdk_plugins(step_types, make)
        pin = git(step_types, "rev-parse", "HEAD")
        pin_date = git(step_types, "log", "-1", "--format=%cI")
    except (DiscoveryError, subprocess.CalledProcessError) as error:
        out.annotation("error", "step-types discovery failed", str(error))
        return 1
    out.table(
        ["#", "Step type", "Settings"],
        [[str(i + 1), name, f"{name}/syntara-plugin.yaml"] for i, name in enumerate(plugins)],
    )
    out.line(f"step-types pin {pin} (committed {pin_date})")
    print(json.dumps(plugins))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"plugins={json.dumps(plugins, separators=(',', ':'))}\n")
            handle.write(f"pin={pin}\n")
            handle.write(f"pin_date={pin_date}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
