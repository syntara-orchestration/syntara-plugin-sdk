"""Run one syntara-step-types plugin against candidate SDK wheels.

Usage::

    python tests/step_types/driver.py --plugin http-request \\
        --step-types-dir ../step-types --wheels-dir dist --report-dir report

The flow under test is step-types' own: the driver runs its ``install``,
``sdk-plugin-validate``, ``sdk-plugin-run``, and ``sdk-plugin-build-inspect``
Make targets unmodified, so a change to that flow on the step-types side is
picked up on the next pin. Around them the driver adds:

1. Candidate injection: every wheel built from the pull request is pointed to
   through ``[tool.uv.sources]``, the lock is regenerated, and the environment
   is synced once. Later targets run with ``UV_NO_SYNC=1``, so their
   ``uv run --frozen`` calls cannot re-sync back to the published SDK.
2. Before the image build, the pristine ``pyproject.toml`` and ``uv.lock`` are
   restored (the step-types Containerfiles copy them into the image, where the
   candidate wheel paths do not exist), and the candidate install is verified
   again.
3. Import tracking: a ``sitecustomize`` hook on ``PYTHONPATH`` records which
   SDK distribution every Python process started by the make targets actually
   imported, so a target that runs the CLI some other way (for example
   ``uv run --with syntara-plugin-sdk==X``) cannot silently test the published
   SDK.
4. Numbered stages, failure classification, an optional mechanical migration,
   and a JSON report.

Exit codes: 0 compatible (or report-only), 1 incompatible, 2 usage error,
3 the check's own tooling failed. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import adapter
from classifier import Classification, Failure, classify, outcome_word, reproduce_hint
from render import (
    BLUE,
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    YELLOW,
    Renderer,
    Section,
    append_summary,
    format_duration,
    md_code,
    md_table,
)

STAGES = ("preflight", "environment", "validate", "run", "registry", "build-inspect")
TOTAL = len(STAGES)
ADAPTED_STAGES = ("validate", "run", "registry", "build-inspect")
MAKE_TARGETS = ("install", "sdk-plugin-validate", "sdk-plugin-run", "sdk-plugin-build-inspect")
REGISTRY_URL = "http://localhost:5000/v2/"
REGISTRY_NAME = "step-types-compat-registry"
REGISTRY_IMAGE = "docker.io/library/registry:2"
STALE_PIN_DAYS = 14
MODES = ("enforcing", "report-only:label", "report-only:dispatch")
REQUIRED_DISTS = ("syntara-plugin-sdk", "syntara-plugin-contracts")
_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_REQUIRES_CONTRACTS = re.compile(
    r"^Requires-Dist:\s*syntara[-_.]plugin[-_.]contracts\s*\(?==\s*([^\s;,)]+)", re.MULTILINE
)
TIMEOUTS = {
    "environment": 600,
    "validate": 300,
    "run": 300,
    "registry": 300,
    "build-inspect": 1200,
}
# Total wall-clock budget for one driver run; kept below the workflow job timeout so a
# hang is still classified and reported instead of being cut off by the runner.
DEFAULT_BUDGET_SECONDS = 2400
MIN_COMMAND_SECONDS = 30
OUTPUT_TAIL = 400
IMPORT_LOG_ENV = "STEP_TYPES_COMPAT_IMPORT_LOG"
_IMPORT_HOOK = '''"""Record which SDK distributions this process imported (step types compat check)."""
import atexit
import json
import os
import sys


def _step_types_compat_record():
    path = os.environ.get("STEP_TYPES_COMPAT_IMPORT_LOG")
    names = [n for n in sys.modules if n == "syntara_plugin" or n.startswith("syntara_plugin.")]
    if not path or not names:
        return
    try:
        import importlib.metadata as metadata

        entry = {"argv": sys.argv[:2]}
        for dist in ("syntara-plugin-sdk", "syntara-plugin-contracts"):
            try:
                found = metadata.distribution(dist)
            except metadata.PackageNotFoundError:
                entry[dist] = None
                continue
            entry[dist] = {"version": found.version, "direct_url": found.read_text("direct_url.json")}
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\\n")
    except Exception:
        pass


atexit.register(_step_types_compat_record)
'''


class StageFailed(Exception):
    """A stage failed; carries what the classifier needs."""

    def __init__(self, note: str, result: CommandResult | None = None, **extra: Any) -> None:
        super().__init__(note)
        self.note = note
        self.result = result
        self.extra = extra


@dataclass
class CommandResult:
    display: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False


@dataclass
class StageRecord:
    index: int
    name: str
    status: str = "pending"
    duration: float | None = None
    note: str = ""
    attempt: int = 1
    commands: list[CommandResult] = field(default_factory=list)
    classification: Classification | None = None
    diagnostic: Any = None
    log: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "status": self.status,
            "duration_s": None if self.duration is None else round(self.duration, 2),
            "note": self.note,
            "attempt": self.attempt,
            "commands": [
                {"command": c.display, "exit_code": c.exit_code, "timed_out": c.timed_out}
                for c in self.commands
            ],
            "log": self.log,
        }


def normalize_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Wheels:
    """The candidate wheels, keyed by normalized distribution name."""

    paths: dict[str, Path]

    def version(self, dist: str) -> str:
        return self.paths[dist].name.split("-")[1]

    @property
    def sdk_version(self) -> str:
        return self.version("syntara-plugin-sdk")

    @property
    def contracts_version(self) -> str:
        return self.version("syntara-plugin-contracts")


def find_wheels(directory: Path) -> Wheels:
    """Collect every candidate wheel and check the SDK/contracts pair is consistent.

    All wheels are injected, not only the two known ones, so a package added to
    the SDK repository later cannot silently resolve from PyPI instead.
    """
    paths: dict[str, Path] = {}
    for wheel in sorted(directory.glob("*.whl")):
        dist = normalize_dist(wheel.name.split("-")[0])
        if dist in paths:
            raise StageFailed(f"candidate wheels: more than one wheel for {dist} in {directory}")
        paths[dist] = wheel.resolve()
    missing = [dist for dist in REQUIRED_DISTS if dist not in paths]
    if missing:
        raise StageFailed(
            f"candidate wheels: {', '.join(missing)} wheel not found in {directory}"
        )
    wheels = Wheels(paths)
    with zipfile.ZipFile(paths["syntara-plugin-sdk"]) as archive:
        name = next((n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), None)
        metadata = archive.read(name).decode("utf-8") if name else ""
    pinned = _REQUIRES_CONTRACTS.search(metadata)
    if pinned and pinned.group(1) != wheels.contracts_version:
        raise StageFailed(
            f"candidate wheels are inconsistent: syntara-plugin-sdk {wheels.sdk_version} "
            f"requires syntara-plugin-contracts=={pinned.group(1)}, but the built contracts "
            f"wheel is {wheels.contracts_version}"
        )
    return wheels


def patch_sources(text: str, sources: dict[str, Path]) -> str:
    """Point ``[tool.uv.sources]`` entries at local wheels, merging with an existing table."""
    entries = [f"{name} = {{ path = {json.dumps(str(path))} }}\n" for name, path in sources.items()]
    lines = text.splitlines(keepends=True)
    header = next((i for i, line in enumerate(lines) if line.strip() == "[tool.uv.sources]"), None)
    if header is None:
        separator = "" if not text or text.endswith("\n") else "\n"
        patched = f"{text}{separator}\n[tool.uv.sources]\n{''.join(entries)}"
    else:
        end = next(
            (i for i in range(header + 1, len(lines)) if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        replaced = {normalize_dist(name) for name in sources}

        def keep(line: str) -> bool:
            # Drop existing entries for the same distributions under any spelling
            # (syntara_plugin_sdk, "syntara-plugin-sdk", ...), or uv sees duplicates.
            match = re.match(r"""\s*["']?([A-Za-z0-9_.\-]+)["']?\s*=""", line)
            return not (match and normalize_dist(match.group(1)) in replaced)

        kept = [line for line in lines[header + 1 : end] if keep(line)]
        patched = "".join(lines[: header + 1] + entries + kept + lines[end:])
    declared = tomllib.loads(patched).get("tool", {}).get("uv", {}).get("sources", {})
    for name, path in sources.items():
        entry = declared.get(name)
        if not isinstance(entry, dict) or entry.get("path") != str(path):
            raise ValueError(f"[tool.uv.sources] does not point {name} at {path}")
    return patched


_VERIFY_CANDIDATE = """
import importlib.metadata as metadata, json, sys
report = {}
for name in sys.argv[1:]:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        report[name] = None
        continue
    report[name] = {"version": dist.version, "direct_url": dist.read_text("direct_url.json")}
print(json.dumps(report))
"""


def _direct_url_info(raw: str | None) -> dict[str, Any]:
    try:
        info = json.loads(raw or "{}")
    except (ValueError, AttributeError):
        return {}
    return info if isinstance(info, dict) else {}


def _is_wheel_url(url: str, wheel: Path) -> bool:
    """Whether ``url`` is the candidate wheel's own file:// location.

    A same-named wheel from anywhere else (a package index, uv's build cache)
    is not the pull request's build, so the comparison is on the full
    location, not the filename.
    """
    if url == wheel.as_uri():
        return True
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "file" and Path(urllib.parse.unquote(parsed.path)) == wheel


def _wheel_mismatch(raw: str | None, wheel: Path) -> str | None:
    """Why an installed distribution is not the candidate wheel, or None."""
    info = _direct_url_info(raw)
    url = str(info.get("url", ""))
    if not _is_wheel_url(url, wheel):
        return f"from {url or 'a package index'}, expected {wheel.as_uri()}"
    archive = info.get("archive_info")
    recorded = str(archive.get("hash", "")) if isinstance(archive, dict) else ""
    for prefix in ("sha256=", "sha256:"):
        if recorded.startswith(prefix):
            if recorded.removeprefix(prefix) != sha256_file(wheel):
                return f"from a different build of {wheel.name} (hash mismatch)"
            break
    return None


def candidate_problems(report: dict[str, Any], wheels: Wheels) -> list[str]:
    """Return why the environment is not running the candidate wheels, if it is not."""
    problems = []
    for dist in REQUIRED_DISTS:
        wheel = wheels.paths[dist]
        entry = report.get(dist)
        if not entry:
            problems.append(f"{dist} is not installed")
            continue
        mismatch = _wheel_mismatch(entry.get("direct_url"), wheel)
        if mismatch:
            problems.append(f"{dist} {entry.get('version', '?')} is installed {mismatch}")
    return problems


def import_problems(log: str, wheels: Wheels) -> list[str]:
    """Return non-candidate SDK imports recorded by the import hook, if any."""
    problems = []
    for line in log.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        command = " ".join(str(part) for part in entry.get("argv", [])) or "a process"
        seen = False
        for dist in REQUIRED_DISTS:
            installed = entry.get(dist)
            if not isinstance(installed, dict):
                continue
            seen = True
            mismatch = _wheel_mismatch(installed.get("direct_url"), wheels.paths[dist])
            if mismatch:
                problems.append(
                    f"{command} imported {dist} {installed.get('version', '?')} {mismatch}"
                )
        if not seen:
            problems.append(f"{command or 'a process'} imported syntara_plugin without an installed "
                            "SDK distribution")
    return sorted(dict.fromkeys(problems))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workload_tag(env: dict[str, str]) -> str:
    if env.get("GITHUB_RUN_ID"):
        attempt = env.get("GITHUB_RUN_ATTEMPT", "1")
        return f"ci-{env['GITHUB_RUN_ID']}-{attempt}-{env.get('GITHUB_SHA', 'unknown')}"
    return f"local-{int(time.time())}"


def registry_alive(url: str = REGISTRY_URL, timeout: float = 2.0) -> bool:
    """True only for an OCI distribution registry (not any HTTP server on the port)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            headers = response.headers
            status = response.status
    except urllib.error.HTTPError as error:
        headers, status = error.headers, error.code
    except (urllib.error.URLError, OSError):
        return False
    return status in (200, 401) and bool(headers.get("Docker-Distribution-Api-Version"))


class Driver:
    def __init__(self, args: argparse.Namespace, out: Renderer) -> None:
        self.args = args
        self.out = out
        self.plugin: str = args.plugin
        self.step_types = Path(args.step_types_dir).resolve()
        self.plugin_dir = self.step_types / self.plugin
        self.report_dir = Path(args.report_dir).resolve()
        self.wheels_dir = Path(args.wheels_dir).resolve()
        self.report_only = args.mode != "enforcing"
        self.started = time.monotonic()
        self.deadline = self.started + args.budget_seconds
        self.sdk_root = Path(__file__).resolve().parents[2]
        self.tools = {name: shutil.which(name) for name in ("uv", "git", "podman", "make", "jq")}
        self.env = self._child_env()
        self.tag = workload_tag(dict(os.environ))
        self.wheels: Wheels | None = None
        self.started_registry = False
        self.records: list[StageRecord] = []
        self.adapted_records: list[StageRecord] = []
        self.adapter_info: dict[str, Any] | None = None
        self.output_lock = threading.Lock()
        self.hook_dir = Path(tempfile.mkdtemp(prefix="step-types-compat-hook-"))
        (self.hook_dir / "sitecustomize.py").write_text(_IMPORT_HOOK, encoding="utf-8")
        self.import_log = self.hook_dir / "imports.jsonl"

    # Environment ------------------------------------------------------------

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        for name in (
            "VIRTUAL_ENV",
            "UV_PROJECT_ENVIRONMENT",
            "PYTHONPATH",
            "UV_SYSTEM_PYTHON",
            "UV_NO_SYNC",
            "UV_NO_SOURCES",
            "UV_FROZEN",
            "UV_LOCKED",
            "FORCE_COLOR",
            IMPORT_LOG_ENV,
        ):
            env.pop(name, None)
        env.update(
            {
                "UV_PYTHON": self.args.python,
                "UV_NO_PROGRESS": "1",
                "NO_COLOR": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        return env

    def tool(self, name: str) -> str:
        path = self.tools.get(name)
        if not path:
            raise StageFailed(f"required tool '{name}' was not found on PATH")
        return path

    def venv_bin(self, name: str) -> str:
        return str(self.step_types / ".venv" / "bin" / name)

    def display(self, args: Sequence[str]) -> str:
        root = str(self.step_types)
        shown = []
        for position, arg in enumerate(args):
            text = str(arg)
            if "\n" in text:
                text = "<inline script>"
            elif text.startswith(root):
                text = "step-types" + text[len(root) :]
            elif position == 0 and "/" in text:
                text = text.rsplit("/", 1)[-1]
            elif text.startswith("UV=/"):
                text = "UV=uv"
            shown.append(text)
        return shlex.join(shown)

    def make(self, target: str, **variables: str) -> list[str]:
        assignments = [f"{key}={value}" for key, value in variables.items()]
        return [self.tool("make"), "--no-print-directory", target, f"UV={self.tool('uv')}", *assignments]

    # Command execution ------------------------------------------------------

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        stage: str,
        title: str,
        no_sync: bool = False,
    ) -> CommandResult:
        """Run one command, streaming its output into a collapsible group.

        ``no_sync`` marks a step-types make target: uv must not re-sync, and the
        import hook records which SDK every Python process imports.
        """
        display = self.display(args)
        env = dict(self.env)
        if no_sync:
            env["UV_NO_SYNC"] = "1"
            env["PYTHONPATH"] = str(self.hook_dir)
            env[IMPORT_LOG_ENV] = str(self.import_log)
            self.import_log.write_text("", encoding="utf-8")
        timeout = max(MIN_COMMAND_SECONDS, min(TIMEOUTS.get(stage, 300), self.remaining()))
        self.out.group_start(f"{title}: {display}")
        self.out.line(self.out.paint(f"$ {'UV_NO_SYNC=1 ' if no_sync else ''}{display}", DIM))
        token = self.out.begin_raw()
        streaming = threading.Event()
        streaming.set()
        start = time.monotonic()
        stdout: list[str] = []
        stderr: list[str] = []
        timed_out = False
        completed = False

        def pump(pipe: Any, sink: list[str], label: str) -> None:
            for line in pipe:
                sink.append(line)
                with self.output_lock:
                    # Never print after the stop-commands region is closed.
                    if streaming.is_set():
                        self.out.raw_line(line, label)
            pipe.close()

        try:
            process = subprocess.Popen(
                [str(a) for a in args],
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            threads = [
                threading.Thread(target=pump, args=(process.stdout, stdout, ""), daemon=True),
                threading.Thread(target=pump, args=(process.stderr, stderr, "err|"), daemon=True),
            ]
            for thread in threads:
                thread.start()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill(process)
            except KeyboardInterrupt:
                self._kill(process)
                raise
            for thread in threads:
                thread.join(timeout=10)
            completed = True
        finally:
            # Always close the stop-commands region (and, on a crash, the group), or
            # the crash handler's own panel and annotation would be inert text.
            with self.output_lock:
                streaming.clear()
                self.out.end_raw(token)
            if not completed:
                self.out.group_end()
        duration = time.monotonic() - start
        status = f"exit code {process.returncode}" + (" (timed out)" if timed_out else "")
        self.out.line(self.out.paint(f"{status} after {format_duration(duration)}", DIM))
        self.out.group_end()
        return CommandResult(
            display=display,
            exit_code=process.returncode,
            stdout="".join(stdout),
            stderr="".join(stderr),
            duration=duration,
            timed_out=timed_out,
        )

    @staticmethod
    def _kill(process: subprocess.Popen[str]) -> None:
        """Kill the command and everything it started (make, uv, podman build, ...)."""
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def checked(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        stage: str,
        title: str,
        record: StageRecord,
        no_sync: bool = False,
    ) -> CommandResult:
        result = self.run(args, cwd=cwd, stage=stage, title=title, no_sync=no_sync)
        record.commands.append(result)
        if no_sync:
            self.verify_imports(result)
        if result.timed_out:
            raise StageFailed(f"{title} timed out after {format_duration(result.duration)}", result)
        if result.exit_code != 0:
            raise StageFailed(f"{title} exited with code {result.exit_code}", result)
        return result

    def verify_imports(self, result: CommandResult) -> None:
        """Fail unless every Python process of a make target imported the candidate SDK.

        A wrong import overrides any other outcome: a result produced by the
        published SDK says nothing about this pull request. A passing target
        that recorded no imports at all also fails, so the hook cannot be
        silently bypassed (for example by ``python -I``).
        """
        if self.wheels is None:
            return
        log = self.import_log.read_text(encoding="utf-8") if self.import_log.exists() else ""
        problems = import_problems(log, self.wheels)
        if problems:
            raise StageFailed("candidate not installed: " + "; ".join(problems), result)
        if not log.strip() and result.exit_code == 0 and not result.timed_out:
            raise StageFailed(
                "candidate not installed: no Python process in the make target reported which "
                "SDK it imported (the import hook did not run)",
                result,
            )

    def verify_candidate(self, record: StageRecord, title: str) -> None:
        """Fail unless the step-types environment runs the candidate wheels."""
        assert self.wheels is not None
        result = self.checked(
            [self.venv_bin("python"), "-c", _VERIFY_CANDIDATE, *REQUIRED_DISTS],
            cwd=self.step_types,
            stage="environment",
            title=title,
            record=record,
        )
        try:
            report = json.loads(result.stdout)
        except ValueError:
            raise StageFailed("candidate not installed: verification printed no JSON", result) from None
        problems = candidate_problems(report, self.wheels)
        if problems:
            raise StageFailed("candidate not installed: " + "; ".join(problems), result)

    # Stages -----------------------------------------------------------------

    def stage_preflight(self, record: StageRecord) -> None:
        required = [
            self.step_types / "pyproject.toml",
            self.step_types / "uv.lock",
            self.step_types / "Makefile",
            self.plugin_dir / "syntara-plugin.yaml",
            self.plugin_dir / "example.json",
        ]
        missing = [str(path.relative_to(self.step_types)) for path in required if not path.is_file()]
        if missing:
            raise StageFailed("missing " + ", ".join(missing))
        makefile = (self.step_types / "Makefile").read_text(encoding="utf-8")
        absent = [t for t in MAKE_TARGETS if not re.search(rf"^{re.escape(t)}:", makefile, re.M)]
        if absent:
            raise StageFailed("Makefile targets missing: " + ", ".join(absent))
        for name in ("uv", "git", "make", "jq", "podman"):
            self.tool(name)
        record.note = "fixture layout, make targets, and tools as expected"

    def stage_environment(self, record: StageRecord) -> None:
        self.wheels = find_wheels(self.wheels_dir)
        pyproject = self.step_types / "pyproject.toml"
        try:
            patched = patch_sources(pyproject.read_text(encoding="utf-8"), self.wheels.paths)
        except (ValueError, tomllib.TOMLDecodeError) as error:
            raise StageFailed(
                f"candidate not installed: could not patch pyproject.toml ({error})"
            ) from None
        pyproject.write_text(patched, encoding="utf-8")
        self.checked(
            [self.tool("uv"), "lock"],
            cwd=self.step_types,
            stage="environment",
            title="Re-lock with candidate wheels",
            record=record,
        )
        self.checked(
            self.make("install"),
            cwd=self.step_types,
            stage="environment",
            title="Install step-types environment (make install)",
            record=record,
        )
        self.verify_candidate(record, "Verify candidate SDK is installed")
        record.note = (
            f"sdk {self.wheels.sdk_version} + contracts {self.wheels.contracts_version} "
            "from candidate wheels"
        )

    def stage_validate(self, record: StageRecord) -> None:
        result = self.run(
            self.make("sdk-plugin-validate", PLUGIN=self.plugin),
            cwd=self.step_types,
            stage="validate",
            title="make sdk-plugin-validate",
            no_sync=True,
        )
        record.commands.append(result)
        self.verify_imports(result)
        if result.exit_code == 0 and not result.timed_out:
            record.note = "validate --json reported ok"
            return
        self.fail_with_diagnostic(
            record,
            result,
            "make sdk-plugin-validate",
            [self.venv_bin("syntara-plugin"), "validate", "--json"],
            "validate",
        )

    def fail_with_diagnostic(
        self,
        record: StageRecord,
        result: CommandResult,
        target: str,
        command: Sequence[str],
        stage: str,
    ) -> None:
        """Re-run a failed target's CLI command once to capture the JSON jq swallowed.

        The make targets pipe ``--json`` output into ``jq -e``, which prints only
        ``false``; compiler diagnostics and run reports live in that document.
        The diagnostic run never decides pass or fail.
        """
        diagnostic = self.run(
            command,
            cwd=self.plugin_dir,
            stage=stage,
            title="Diagnostic run (captures the JSON the target pipes into jq)",
        )
        record.commands.append(diagnostic)
        try:
            document = json.loads(diagnostic.stdout)
        except ValueError:
            document = None
        record.diagnostic = document
        note = f"{target} " + (
            "timed out" if result.timed_out else f"exited with code {result.exit_code}"
        )
        raise StageFailed(note, result, diagnostic=document)

    def stage_run(self, record: StageRecord) -> None:
        result = self.run(
            self.make("sdk-plugin-run", PLUGIN=self.plugin),
            cwd=self.step_types,
            stage="run",
            title="make sdk-plugin-run",
            no_sync=True,
        )
        record.commands.append(result)
        self.verify_imports(result)
        if result.exit_code == 0 and not result.timed_out:
            record.note = "run output matched step-types' expectation"
            return
        self.fail_with_diagnostic(
            record,
            result,
            "make sdk-plugin-run",
            [self.venv_bin("syntara-plugin"), "run", "--mode", "source", "--input-file",
             "example.json", "--json"],
            "run",
        )

    def stage_registry(self, record: StageRecord) -> None:
        if registry_alive():
            record.note = "reusing the registry already listening on localhost:5000"
            return
        podman = self.tool("podman")
        subprocess.run(
            [podman, "rm", "--force", REGISTRY_NAME], env=self.env, capture_output=True, check=False
        )
        # Set before starting: a failed `podman run` can leave a Created container behind.
        self.started_registry = True
        self.checked(
            [podman, "run", "--detach", "--name", REGISTRY_NAME, "--publish", "5000:5000",
             REGISTRY_IMAGE],
            cwd=self.step_types,
            stage="registry",
            title="Start temporary OCI registry",
            record=record,
        )
        for _ in range(30):
            if registry_alive():
                record.note = "temporary registry started on localhost:5000"
                return
            time.sleep(1)
        logs = subprocess.run(
            [podman, "logs", REGISTRY_NAME], env=self.env, capture_output=True, text=True,
            check=False,
        )
        raise StageFailed(
            "registry did not answer on localhost:5000 within 30s; "
            + (logs.stdout + logs.stderr).strip()[-400:]
        )

    def stage_build_inspect(self, record: StageRecord) -> None:
        # The Containerfiles copy pyproject.toml and uv.lock into the image, where the
        # candidate wheel paths do not exist; build with the pristine pair. The CLI keeps
        # running from the environment synced in the environment stage (UV_NO_SYNC=1).
        self.checked(
            [self.tool("git"), "-C", str(self.step_types), "checkout", "--", "pyproject.toml",
             "uv.lock"],
            cwd=self.step_types,
            stage="build-inspect",
            title="Restore pristine pyproject.toml and uv.lock",
            record=record,
        )
        self.verify_candidate(record, "Re-verify candidate SDK before the image build")
        result = self.run(
            self.make("sdk-plugin-build-inspect", PLUGIN=self.plugin, SDK_WORKLOAD_TAG=self.tag),
            cwd=self.step_types,
            stage="build-inspect",
            title="make sdk-plugin-build-inspect",
            no_sync=True,
        )
        record.commands.append(result)
        self.verify_imports(result)
        if result.exit_code == 0 and not result.timed_out:
            record.note = "archive built, inspected, and matched to its source"
            return
        # The target redirects the build and inspect JSON into dist/; the later one
        # that exists is the document behind the failure (no re-run needed).
        document = None
        for suffix in ("inspect", "build"):
            path = self.plugin_dir / "dist" / f"{self.plugin}-{self.tag}-{suffix}.json"
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                continue
        record.diagnostic = document
        note = "make sdk-plugin-build-inspect " + (
            "timed out" if result.timed_out else f"exited with code {result.exit_code}"
        )
        raise StageFailed(note, result, diagnostic=document)

    # Ladder -----------------------------------------------------------------

    def run_ladder(self, names: Sequence[str], *, adapted: bool) -> list[StageRecord]:
        records: list[StageRecord] = []
        blocked_by = ""
        for name in names:
            index = STAGES.index(name) + 1
            record = StageRecord(index=index, name=name)
            records.append(record)
            label = f"{name} (adapted)" if adapted else name
            if blocked_by:
                record.status = "skip"
                record.note = f"blocked by {blocked_by}"
                self.out.stage_result(index, TOTAL, label, "skip", note=record.note)
                continue
            self.out.stage_start(index, TOTAL, label, self._stage_hint(name))
            self._execute(record, label)
            self.out.stage_result(
                index,
                TOTAL,
                label,
                record.status,
                duration=record.duration,
                elapsed=time.monotonic() - self.started,
                note=self._result_note(record),
            )
            if record.status == "fail":
                blocked_by = name
        return records

    def _execute(self, record: StageRecord, label: str) -> None:
        stage = getattr(self, f"stage_{record.name.replace('-', '_')}")
        while True:
            start = time.monotonic()
            try:
                stage(record)
                record.status = "pass"
            except StageFailed as failed:
                record.status = "fail"
                record.note = failed.note
                result = failed.result
                failure = Failure(
                    stage=record.name,
                    plugin=self.plugin,
                    command=result.display if result else "",
                    exit_code=result.exit_code if result else None,
                    stdout=result.stdout if result else "",
                    stderr=result.stderr if result else "",
                    note=failed.note,
                    timed_out=bool(result and result.timed_out),
                    diagnostic=failed.extra.get("diagnostic"),
                    plugin_dir=self.plugin_dir,
                    step_types_dir=self.step_types,
                )
                record.classification = classify(failure)
            record.duration = time.monotonic() - start
            classification = record.classification
            if (
                record.status == "fail"
                and classification is not None
                and (
                    classification.category == "infra"
                    or bool(classification.facts.get("network_signature"))
                )
                and classification.rule_id != "IN002"  # never repeat a timeout
                and record.attempt == 1
                and self.remaining() > TIMEOUTS.get(record.name, 300)
            ):
                self.out.annotation(
                    "warning",
                    f"step-types compat {self.out.dot} {self.plugin} {self.out.dot} {record.name}",
                    f"{classification.rule_id} {classification.rule_name}: retrying once",
                )
                self.out.stage_start(record.index, TOTAL, f"{label} (retry)", "infrastructure retry")
                record.attempt = 2
                record.commands.clear()
                record.classification = None
                record.diagnostic = None
                continue
            return

    def _stage_hint(self, name: str) -> str:
        return {
            "preflight": "check the pinned step-types layout and tools",
            "environment": "inject candidate wheels, re-lock, make install, verify",
            "validate": "make sdk-plugin-validate",
            "run": "make sdk-plugin-run (source mode)",
            "registry": "local OCI registry on localhost:5000",
            "build-inspect": "restore pristine lock, re-verify, make sdk-plugin-build-inspect",
        }[name]

    def _result_note(self, record: StageRecord) -> str:
        if record.status == "fail" and record.classification is not None:
            c = record.classification
            return f"{c.category} ({c.rule_id} {c.rule_name})"
        return record.note if record.status == "pass" else ""

    # Adapter ----------------------------------------------------------------

    def try_adapter(self, failed: StageRecord) -> None:
        classification = failed.classification
        if classification is None or classification.category != "misconfiguration":
            return
        transform_name = classification.facts.get("transform")
        if not transform_name or self.args.no_adapter:
            return
        transform = adapter.TRANSFORMS[transform_name]
        self.out.blank()
        self.out.rule(f"Adapter: {transform.title}", BLUE)
        info: dict[str, Any] = {
            "transform": transform.name,
            "title": transform.title,
            "caveat": transform.caveat,
        }
        self.adapter_info = info
        try:
            changed = transform.apply(self.plugin_dir)
        except (adapter.NotApplicable, ValueError, OSError) as error:
            info.update(outcome="not-applicable", reason=str(error))
            self.out.line(self.out.paint(f"Transform not applicable: {error}", YELLOW))
            return
        info["changed"] = changed
        self.out.line(
            f"Applied to {', '.join(f'{self.plugin}/{name}' for name in changed)}; "
            "re-running the remaining stages on the migrated copy (nothing is committed)."
        )
        diff = subprocess.run(
            [self.tool("git"), "-C", str(self.step_types), "diff", "--no-color", "--",
             *(f"{self.plugin}/{name}" for name in changed)],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        self.adapted_records = self.run_ladder(ADAPTED_STAGES, adapted=True)
        patch_name = f"{self.plugin}-migration.patch"
        (self.report_dir / patch_name).write_text(diff, encoding="utf-8")
        info["patch"] = patch_name
        info["patch_text"] = diff
        residual = next((r for r in self.adapted_records if r.status == "fail"), None)
        info["outcome"] = "failed" if residual else "passed"
        if residual is not None and residual.classification is not None:
            info["residual"] = {"stage": residual.name, **residual.classification.to_dict()}

    # Reporting --------------------------------------------------------------

    def pin_info(self) -> dict[str, Any]:
        git = self.tools.get("git")
        info: dict[str, Any] = {"sha": "unknown", "date": "", "age_days": None}
        if not git:
            return info
        def ask(*args: str) -> str:
            return subprocess.run(
                [git, "-C", str(self.step_types), *args], capture_output=True, text=True,
                check=False,
            ).stdout.strip()
        info["sha"] = ask("rev-parse", "HEAD") or "unknown"
        info["date"] = ask("log", "-1", "--format=%cI")
        if info["date"]:
            committed = dt.datetime.fromisoformat(info["date"])
            info["age_days"] = (dt.datetime.now(dt.UTC) - committed).days
        return info

    def tool_versions(self) -> str:
        parts = [f"Python {sys.version.split()[0]}"]
        for name in ("uv", "podman"):
            path = self.tools.get(name)
            if not path:
                parts.append(f"{name} not found")
                continue
            output = subprocess.run([path, "--version"], capture_output=True, text=True, check=False)
            parts.append(output.stdout.strip() or f"{name} (version unknown)")
        return ", ".join(parts)

    def sdk_commit(self) -> str:
        sha = os.environ.get("GITHUB_SHA", "")
        if not sha and self.tools.get("git"):
            sha = subprocess.run(
                [self.tools["git"], "-C", str(self.sdk_root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        return sha[:12] or "unknown"

    def mode_label(self) -> str:
        return {
            "enforcing": "enforcing (failures fail the check)",
            "report-only:label": "report-only (pull request has the breaking-change label)",
            "report-only:dispatch": "report-only (manual run with report_only)",
        }[self.args.mode]

    def header(self, pin: dict[str, Any]) -> None:
        try:
            wheels = find_wheels(self.wheels_dir)
            candidate = (
                f"syntara-plugin-sdk {wheels.sdk_version}, "
                f"syntara-plugin-contracts {wheels.contracts_version}"
            )
        except StageFailed as error:
            candidate = error.note
        age = pin["age_days"]
        age_text = f"{age} day(s) old" if age is not None else "age unknown"
        self.out.banner(
            f"Step types compatibility {self.out.dot} {self.plugin}",
            [
                ("Step type", self.plugin),
                ("SDK candidate", candidate),
                ("SDK commit", self.sdk_commit()),
                ("step-types pin", f"{pin['sha'][:12]} ({age_text}, committed {pin['date'][:10]})"),
                ("Mode", self.mode_label()),
                ("Tools", self.tool_versions()),
                ("Workload tag", self.tag),
            ],
        )
        if age is not None and age > STALE_PIN_DAYS:
            self.out.annotation(
                "warning",
                "step-types pin is stale",
                f"The pinned syntara-step-types revision {pin['sha'][:12]} is {age} days old; "
                "consider bumping STEP_TYPES_REF in .github/workflows/step-types-compat.yml. "
                "Compare: https://github.com/syntara-orchestration/syntara-step-types/compare/"
                f"{pin['sha']}...devel",
            )
        self.out.blank()

    def write_logs(self, records: Sequence[StageRecord], suffix: str = "") -> None:
        for record in records:
            if not record.commands:
                continue
            name = f"{self.plugin}-{record.index}-{record.name}{suffix}.log"
            chunks = []
            for command in record.commands:
                chunks.append(
                    f"$ {command.display}\n[exit code {command.exit_code}"
                    f"{', timed out' if command.timed_out else ''}, "
                    f"{format_duration(command.duration)}]\n"
                    f"--- stdout ---\n{command.stdout}\n--- stderr ---\n{command.stderr}\n"
                )
            (self.report_dir / name).write_text("\n".join(chunks), encoding="utf-8")
            record.log = name

    def stage_rows(self, records: Sequence[StageRecord]) -> tuple[list[list[str]], list[str | None]]:
        rows, colors = [], []
        for record in records:
            word = {"pass": "pass", "fail": "FAIL", "skip": "skip"}.get(record.status, record.status)
            glyph = self.out.glyphs.get(record.status, "?")
            note = self._result_note(record) if record.status != "skip" else record.note
            if record.attempt > 1:
                note = f"{note} (after retry)".strip()
            rows.append(
                [f"{record.index}/{TOTAL}", record.name, f"{glyph} {word}",
                 format_duration(record.duration), note]
            )
            colors.append({"pass": GREEN, "fail": RED, "skip": YELLOW}.get(record.status))
        return rows, colors

    def failure_panel(self, record: StageRecord) -> None:
        c = record.classification
        assert c is not None
        dot = self.out.glyphs["dot"]
        sections = [
            Section(
                "Summary",
                [
                    f"Stage      [{record.index}/{TOTAL}] {record.name}",
                    f"Category   {c.category}",
                    f"Analysis   {c.analysis}",
                    f"Origin     {c.origin}",
                ],
            ),
            Section("What happened", [c.what]),
        ]
        if c.evidence:
            sections.append(Section("Evidence (verbatim)", list(c.evidence), verbatim=True))
        sections.append(Section("Likely cause", [c.cause]))
        actions = [f"{dot} {action}" for action in (*c.actions, reproduce_hint(self.plugin))]
        sections.append(Section("What to do", actions))
        if record.log:
            sections.append(
                Section("Full log", [f"artifact step-types-compat-{self.plugin}: {record.log}"])
            )
        self.out.panel(f"FAILURE {dot} {self.plugin} {dot} {record.name}", sections, tone=RED)

    def extra_logs(self, record: StageRecord) -> None:
        failing = [c for c in record.commands if c.exit_code != 0 or c.timed_out]
        if failing:
            command = failing[0]
            self.out.group_start(f"Failed command output: {command.display}")
            self.out.line(f"exit code {command.exit_code}{' (timed out)' if command.timed_out else ''}")
            for label, text in (("stdout", command.stdout), ("stderr", command.stderr)):
                lines = text.splitlines()
                shown = lines[-OUTPUT_TAIL:]
                suffix = f" (last {len(shown)} of {len(lines)} lines)" if len(lines) > len(shown) else ""
                self.out.rule(f"{label}{suffix}")
                self.out.raw_block(shown or ["(empty)"])
            self.out.group_end()
        if record.diagnostic is not None:
            self.out.group_start("Captured --json document behind the failure")
            self.out.raw_block(json.dumps(record.diagnostic, indent=2).splitlines())
            self.out.group_end()
        c = record.classification
        if c is not None and c.frames:
            self.out.group_start("Traceback analysis (deepest frame first)")
            self.out.table(
                ["#", "Side", "Location", "Function"],
                [
                    [str(i + 1), frame.origin, f"{frame.path}:{frame.line}", frame.function]
                    for i, frame in enumerate(c.frames[:25])
                ],
            )
            self.out.group_end()
        self.out.group_start("Environment diagnostics")
        lines = [f"step-types checkout: {self.step_types}", f"workload tag: {self.tag}"]
        if self.wheels is not None:
            for wheel in sorted(self.wheels.paths.values()):
                lines.append(f"candidate wheel: {wheel.name} sha256={sha256_file(wheel)}")
        python = Path(self.venv_bin("python"))
        if python.exists() and self.tools.get("uv"):
            listed = subprocess.run(
                [self.tools["uv"], "pip", "list", "--python", str(python)],
                capture_output=True,
                text=True,
                check=False,
                env=self.env,
            ).stdout
            lines.extend(line for line in listed.splitlines() if "syntara" in line.lower())
        self.out.raw_block(lines)
        self.out.group_end()

    def adapter_report(self) -> None:
        info = self.adapter_info
        if not info:
            return
        outcome = info.get("outcome")
        tone = GREEN if outcome == "passed" else YELLOW
        sections = [Section("Transform", [info["title"]])]
        if outcome == "not-applicable":
            sections.append(Section("Outcome", [f"Not applicable: {info.get('reason', '')}"]))
        else:
            verdict = (
                "The migrated copy passes every remaining stage: the break is mechanically "
                "migratable with the attached patch."
                if outcome == "passed"
                else "The migration fixes the reported settings, but the migrated copy still "
                "fails later: more manual migration is needed (residual failure below)."
            )
            sections.append(Section("Outcome", [verdict]))
            sections.append(Section("Caveat", [info["caveat"]]))
            changed = [f"{self.plugin}/{name}" for name in info.get("changed", [])]
            sections.append(Section("Changed files", changed))
            sections.append(
                Section(
                    "Migration patch",
                    [f"artifact step-types-compat-{self.plugin}: {info.get('patch')}"],
                )
            )
            residual = info.get("residual")
            if residual:
                sections.append(
                    Section(
                        "Residual failure",
                        [
                            f"[{STAGES.index(residual['stage']) + 1}/{TOTAL}] {residual['stage']}: "
                            f"{residual['category']} ({residual['rule_id']} {residual['rule_name']})",
                            residual["what"],
                        ],
                    )
                )
        self.out.panel(f"ADAPTER {self.out.glyphs['dot']} {self.plugin}", sections, tone=tone)
        if info.get("patch_text"):
            self.out.group_start(f"Migration patch ({info['patch']})")
            self.out.raw_block(info["patch_text"].splitlines())
            self.out.group_end()

    def summary_markdown(self, failed: StageRecord | None, pin: dict[str, Any]) -> str:
        words = {"pass": "pass", "fail": "FAIL", "skip": "skip"}
        rows = [
            [f"{r.index}/{TOTAL}", r.name, words.get(r.status, r.status),
             format_duration(r.duration), self._result_note(r) or r.note]
            for r in self.records
        ]
        if failed is None:
            title = "compatible"
        else:
            category = failed.classification.category if failed.classification else "unknown"
            title = f"{outcome_word(category)} ({category})" + (
                ", report-only" if self.report_only else ""
            )
        parts = [
            f"### `{self.plugin}`: {title}\n\n",
            f"Pin `{pin['sha'][:12]}` ({pin['age_days']} days old) {self.out.dot} "
            f"mode: {self.mode_label()}\n\n",
            md_table(["Stage", "Name", "Result", "Duration", "Notes"], rows),
        ]
        if failed is not None and failed.classification is not None:
            c = failed.classification
            body = [
                f"**Category:** {c.category}  \n**Analysis:** {c.analysis}  \n"
                f"**Origin:** {c.origin}\n",
                f"**What happened:** {c.what}\n",
                ("**Evidence:**\n\n" + md_code("\n".join(c.evidence))) if c.evidence else "",
                f"**Likely cause:** {c.cause}\n",
                "**What to do:**\n"
                + "\n".join(f"- {a}" for a in (*c.actions, reproduce_hint(self.plugin)))
                + "\n",
            ]
            info = self.adapter_info
            if info and info.get("outcome") in ("passed", "failed"):
                body.append(
                    f"**Adapter:** {info['title']}: {info['outcome']}. {info['caveat']} Patch in "
                    f"artifact `step-types-compat-{self.plugin}` (`{info['patch']}`).\n"
                )
                if info.get("patch_text"):
                    body.append("\n" + md_code(info["patch_text"].rstrip()))
            parts.append(
                "\n<details><summary>Failure analysis</summary>\n\n"
                + "\n".join(body)
                + "\n</details>\n"
            )
        return "".join(parts) + "\n"

    def report(self, pin: dict[str, Any]) -> int:
        self.write_logs(self.records)
        self.write_logs(self.adapted_records, suffix="-adapted")
        failed = next((r for r in self.records if r.status == "fail"), None)
        self.out.blank()
        self.out.rule("Result", CYAN)
        headers = ["Stage", "Name", "Result", "Duration", "Notes"]
        rows, colors = self.stage_rows(self.records)
        self.out.table(headers, rows, aligns="lllrl", row_colors=colors)
        if self.adapted_records:
            self.out.line(self.out.paint("Adapted re-run (migrated copy):", BOLD))
            rows, colors = self.stage_rows(self.adapted_records)
            self.out.table(headers, rows, aligns="lllrl", row_colors=colors)
        if failed is not None:
            self.out.blank()
            self.failure_panel(failed)
            self.extra_logs(failed)
            self.adapter_report()
        elapsed = format_duration(time.monotonic() - self.started)
        glyphs = self.out.glyphs
        self.out.blank()
        if failed is None:
            self.out.line(
                self.out.paint(
                    f"{glyphs['pass']} {self.plugin}: compatible with the candidate SDK ({elapsed})",
                    BOLD,
                    GREEN,
                )
            )
        else:
            c = failed.classification
            assert c is not None
            verdict = (
                f"{self.plugin}: {outcome_word(c.category)} at [{failed.index}/{TOTAL}] "
                f"{failed.name}, {c.category} ({c.rule_id} {c.rule_name})"
            )
            if self.report_only:
                text = f"{glyphs['warn']} {verdict}; report-only, not failing ({elapsed})"
                self.out.line(self.out.paint(text, BOLD, YELLOW))
            else:
                self.out.line(self.out.paint(f"{glyphs['fail']} {verdict} ({elapsed})", BOLD, RED))
            self.out.annotation(
                "warning" if self.report_only else "error",
                f"step-types compat {self.out.dot} {self.plugin} {self.out.dot} {failed.name}",
                f"{c.category} ({c.rule_id} {c.rule_name}): {c.what}",
            )
        document = {
            "schema": 1,
            "plugin": self.plugin,
            "result": "pass" if failed is None else "fail",
            "mode": self.args.mode,
            "report_only": self.report_only,
            "pin": pin,
            "sdk": {
                "commit": self.sdk_commit(),
                "sdk_version": self.wheels.sdk_version if self.wheels else None,
                "contracts_version": self.wheels.contracts_version if self.wheels else None,
            },
            "workload_tag": self.tag,
            "stages": [r.to_dict() for r in self.records],
            "failure": None
            if failed is None or failed.classification is None
            else {"stage": failed.name, "index": failed.index, **failed.classification.to_dict()},
            "adapter": None
            if not self.adapter_info
            else {k: v for k, v in self.adapter_info.items() if k != "patch_text"},
            "adapted_stages": [r.to_dict() for r in self.adapted_records],
        }
        (self.report_dir / f"{self.plugin}.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
        append_summary(self.summary_markdown(failed, pin))
        if failed is None or self.report_only:
            return 0
        return 1

    # Lifecycle --------------------------------------------------------------

    def cleanup(self) -> None:
        git = self.tools.get("git")
        if git and (self.step_types / ".git").exists():
            # Leave the checkout as found: pristine lock, no migration edits, no build output.
            for args in (
                ["checkout", "--", "pyproject.toml", "uv.lock", self.plugin],
                ["clean", "-fdq", "--", f"{self.plugin}/dist"],
            ):
                subprocess.run(
                    [git, "-C", str(self.step_types), *args], capture_output=True, check=False
                )
        if self.started_registry and self.tools.get("podman") and not self.args.keep_registry:
            subprocess.run(
                [self.tools["podman"], "rm", "--force", REGISTRY_NAME],
                capture_output=True,
                check=False,
            )
        shutil.rmtree(self.hook_dir, ignore_errors=True)

    def main(self) -> int:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        pin = self.pin_info()
        self.header(pin)
        try:
            self.records = self.run_ladder(STAGES, adapted=False)
            failed = next((r for r in self.records if r.status == "fail"), None)
            if failed is not None:
                self.try_adapter(failed)
            return self.report(pin)
        finally:
            self.cleanup()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin", required=True, help="step-types plugin name")
    parser.add_argument("--step-types-dir", required=True, help="syntara-step-types git checkout")
    parser.add_argument("--wheels-dir", required=True, help="directory with the candidate wheels")
    parser.add_argument("--report-dir", required=True, help="where reports and logs are written")
    parser.add_argument("--mode", choices=MODES, default="enforcing")
    parser.add_argument("--python", default="3.12", help="Python version for the step-types env")
    parser.add_argument("--no-adapter", action="store_true", help="skip mechanical migrations")
    parser.add_argument(
        "--keep-registry", action="store_true", help="leave a registry this run started running"
    )
    parser.add_argument(
        "--budget-seconds",
        type=int,
        default=DEFAULT_BUDGET_SECONDS,
        help="wall-clock budget for the whole run; commands are stopped when it runs out",
    )
    args = parser.parse_args(argv)
    if not _PLUGIN_NAME.fullmatch(args.plugin):
        # The name becomes report and artifact file names; never let it name a path.
        parser.error(f"invalid plugin name {args.plugin!r} (expected {_PLUGIN_NAME.pattern})")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    out = Renderer()
    try:
        return Driver(args, out).main()
    except KeyboardInterrupt:
        out.line("Interrupted; the running command and its children were stopped.")
        return 130
    except Exception:  # A tooling crash is exit 3, never a compatibility verdict.
        out.blank()
        out.panel(
            f"CHECK TOOLING ERROR {out.glyphs['dot']} {args.plugin}",
            [
                Section(
                    "What happened",
                    [
                        "The compatibility check's own tooling crashed. This is not a "
                        "compatibility verdict about the pull request."
                    ],
                ),
                Section("Traceback", traceback.format_exc().splitlines(), verbatim=True),
            ],
            tone=RED,
        )
        out.annotation(
            "error",
            f"step-types compat {out.dot} {args.plugin} {out.dot} tooling error",
            "The check's tooling crashed; see the log. Not a compatibility verdict.",
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
