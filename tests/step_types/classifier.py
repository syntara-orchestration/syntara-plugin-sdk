"""Deterministic failure classification for the step types compatibility check.

Rules are ordered, pure functions over one captured failure; the first match
wins, so the same failure always produces the same analysis. Each rule has a
stable ID and a confidence level:

- ``deterministic``: an exact signature from text or JSON the SDK, uv, make,
  jq, or the check itself emits (for a failed run, the run document's
  ``error.code`` or ``error.kind``);
- ``inferred``: located from traceback provenance or a broad signal (the
  location is a fact, the cause is an inference);
- ``unclassified``: nothing matched.

Categories reuse the failure-origin vocabulary planned for the SDK's own error
reporting (``misconfiguration``, ``contract``, ``implementation``, ``unknown``)
plus operational extras (``sdk-regression``, ``infra``, ``fixture-drift``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

BREAKING_LABEL = "breaking-change"

ORIGINS = {
    "misconfiguration": "step-types plugin workspace settings, rejected by the candidate SDK",
    "contract": "the boundary between the candidate SDK and step-types "
    "(dependencies, CLI arguments, CLI output, input validation, or the runtime ABI)",
    "implementation": "step-types code or behaviour (the failure is located there; the cause "
    "may be a behaviour change in this pull request)",
    "sdk-regression": "candidate SDK code or packaging",
    "infra": "the machine running the check (tools, registry, container engine, or network)",
    "fixture-drift": "the pinned step-types checkout no longer matches what this check expects",
    "unknown": "not determined",
}

INTENTIONAL = (
    f"Intentional change? Add the `{BREAKING_LABEL}` label to the pull request; the check "
    "re-runs by itself and then reports without failing. Share this report (and the migration "
    "patch, when one is attached) with the step-types maintainers."
)
_REPRODUCE = "Reproduce locally: make test-step-types STEP_TYPES_PLUGIN={plugin}"

# Run document fields that echo the step's input or output; never match signatures
# inside them: a status string or script the step produced is data, not evidence.
_ECHO_KEYS = frozenset({"input", "inputSource", "output"})
_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<function>[^\s\\"]+)')
_EXCEPTION_LINE = re.compile(r"^(?:[A-Za-z_][\w.]*\.)?[A-Z_]\w*(?:Error|Exception|Exit|Failure)\b.*$")
_UNSUPPORTED = re.compile(
    r"(?P<label>[A-Za-z0-9_.\-/\[\] ]+) contains unsupported setting\(s\): (?P<keys>[^\n\"]+)"
)
_SCHEMA = re.compile(r"settings document schema validation failed: (?P<detail>[^\n\"]+)")
_REQUIRES = re.compile(r"(?P<label>[A-Za-z0-9_.\-/\[\] ]+) requires: (?P<keys>[^\n\"]+)")
_CLI_USAGE = re.compile(
    r"syntara-plugin(?: [a-z-]+)?: error: (?P<detail>unrecognized arguments: [^\n]+"
    r"|argument [^\n]+: invalid choice[^\n]*|the following arguments are required: [^\n]+)"
)
_INFRA_SIGNATURES = (
    "Cannot connect to Podman",
    "no space left on device",
    "toomanyrequests",
    "TLS handshake timeout",
    "i/o timeout",
    "Temporary failure in name resolution",
    "Temporary DNS failure",
    "Name or service not known",
    "Network is unreachable",
    "connection reset by peer",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
)
_RESOLUTION_SIGNATURES = ("No solution found when resolving dependencies", "is unsatisfiable")
_RESOLUTION_DETAIL = (
    "No solution found",
    "there is no version of",
    "depends on",
    "requires-python",
    "Python>=",
    "unsatisfiable",
)
_ABI_SIGNATURE = "does not support the requested runtime ABI"
_GRPC_SIGNATURE = re.compile(r"StatusCode\.[A-Z_]+|grpc\._channel|_InactiveRpcError")
_MAKE_ERROR = re.compile(r"^make(\[\d+\])?: \*\*\*", re.MULTILINE)

# FailureCode values a step reports through the SDK runtime (syntara_plugin.runtime).
# FailureCode values a step reports through the SDK runtime (syntara_plugin.runtime),
# mapped by meaning for a failed run document without a traceback (a traceback
# classifies by provenance first, in every stage). Every member of the SDK's
# FailureCode enum belongs in exactly one set; test_step_types_classifier_sync.py
# fails when the SDK adds a code without a mapping, and a code missing here
# classifies as unknown rather than being guessed.
_TRANSIENT_CODES = frozenset({"UPSTREAM_TRANSIENT", "RATE_LIMITED", "TIMEOUT"})
_BOUNDARY_CODES = frozenset({"ABI_VIOLATION", "CREDENTIAL_SCAN_LIMIT"})
_INPUT_CODES = frozenset({"INVALID_INPUT"})
_STEP_REPORTED_CODES = frozenset({
    "REMOTE_REJECTED",
    "POLICY_DENIED",
    "NOT_FOUND",
    "CONFLICT",
    "AUTHENTICATION_FAILED",
    "AUTHORIZATION_FAILED",
})
# CANCELLED is undetermined: the SDK reports it both for step-reported
# cancellations and for cancelled transport RPCs, and only a traceback
# (classified first, by provenance) tells them apart.
_UNDETERMINED_CODES = frozenset({"OUTCOME_UNKNOWN", "CANCELLED"})
_SETTINGS_KINDS = frozenset({"SettingsError", "CompilationError"})


@dataclass(frozen=True)
class Failure:
    """Everything captured about one failed stage.

    ``diagnostic`` holds the decoded ``--json`` document behind a failed make
    target: a diagnostic re-run of ``validate`` or ``run`` (the targets pipe that
    JSON into jq, so their own output only says ``false``), or the build or
    inspect JSON the build-inspect target wrote to ``dist/``.
    """

    stage: str
    plugin: str
    command: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    note: str = ""
    timed_out: bool = False
    diagnostic: Any = None
    plugin_dir: Path | None = None
    step_types_dir: Path | None = None

    @property
    def text(self) -> str:
        return f"{self.stderr}\n{self.stdout}\n{self.note}"


@dataclass(frozen=True)
class Frame:
    """One traceback frame with the side of the boundary it belongs to."""

    path: str
    line: int
    function: str
    origin: str


@dataclass(frozen=True)
class Classification:
    """The analysis printed in the failure panel and stored in the report."""

    category: str
    rule_id: str
    rule_name: str
    confidence: str
    what: str
    evidence: tuple[str, ...]
    cause: str
    actions: tuple[str, ...]
    facts: Mapping[str, Any] = field(default_factory=dict)
    frames: tuple[Frame, ...] = ()

    @property
    def origin(self) -> str:
        return ORIGINS[self.category]

    @property
    def analysis(self) -> str:
        if self.confidence == "unclassified":
            return "unclassified"
        return f"{self.confidence}, rule {self.rule_id} {self.rule_name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "confidence": self.confidence,
            "origin": self.origin,
            "what": self.what,
            "evidence": list(self.evidence),
            "cause": self.cause,
            "actions": list(self.actions),
            "facts": dict(self.facts),
            "frames": [frame.__dict__ for frame in self.frames],
        }


# Helpers ---------------------------------------------------------------------


def _strings(value: Any) -> Iterator[str]:
    """Yield every string inside a decoded JSON document, skipping input echoes."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if key not in _ECHO_KEYS:
                yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _decode(text: str) -> Any:
    candidate = text.strip()
    if not candidate.startswith("{"):
        return None
    try:
        return json.loads(candidate)
    except ValueError:
        return None


def captured_document(failure: Failure) -> Mapping[str, Any] | None:
    """Return the decoded ``--json`` document captured for ``failure``, if there is one."""
    for document in (failure.diagnostic, _decode(failure.stdout)):
        if isinstance(document, Mapping):
            return document
    return None


def _searchable(failure: Failure) -> str:
    """Return the failure text plus the strings inside JSON documents.

    ``run --json`` reports errors as a JSON document, where newlines inside
    messages and tracebacks are escaped; decoding makes them matchable. The
    step's echoed input is excluded so a URL or script inside the example input
    cannot trigger a rule.
    """
    stdout = failure.stdout if _decode(failure.stdout) is None else ""
    parts = [failure.stderr, stdout, failure.note]
    for document in (_decode(failure.stdout), _decode(failure.stderr), failure.diagnostic):
        if document is not None:
            parts.extend(_strings(document))
    return "\n".join(parts)


def _error(document: Mapping[str, Any] | None) -> Mapping[str, Any]:
    error = document.get("error") if document else None
    return error if isinstance(error, Mapping) else {}


def _matching_lines(text: str, needles: str | Sequence[str], limit: int = 3) -> tuple[str, ...]:
    wanted = (needles,) if isinstance(needles, str) else tuple(needles)
    lines = [line.strip() for line in text.splitlines() if any(n in line for n in wanted)]
    return tuple(dict.fromkeys(lines))[:limit]


def _exception_line(text: str) -> str | None:
    """Return the last ``SomeError: message`` line of a traceback, if any."""
    for line in reversed(text.splitlines()):
        if _EXCEPTION_LINE.match(line):
            return line.strip()
    return None


def _jq_false(failure: Failure) -> bool:
    """True when a jq ``-e`` expectation in a make target evaluated to false."""
    lines = [line.strip() for line in failure.stdout.splitlines() if line.strip()]
    return bool(lines) and lines[-1] == "false" and bool(_MAKE_ERROR.search(failure.stderr))


def frame_origin(path: str, step_types_dir: Path | None = None) -> str:
    """Label one traceback frame path by the side of the boundary it is on."""
    normalized = path.replace("\\", "/")
    if "/syntara_plugin/" in normalized:
        return "sdk"
    if "/syntara_node_" in normalized:
        return "step-types"
    if step_types_dir is not None:
        root = str(step_types_dir.resolve()).replace("\\", "/").rstrip("/") + "/"
        if normalized.startswith(root) and "/.venv/" not in normalized:
            return "step-types"
    if "site-packages" in normalized or "dist-packages" in normalized:
        return "third-party"
    if normalized.startswith("<") or re.search(r"/lib/python3\.\d+/", normalized):
        return "stdlib"
    return "unknown"


def parse_frames(failure: Failure) -> tuple[Frame, ...]:
    """Extract traceback frames, deepest first, deduplicated."""
    frames: list[Frame] = []
    seen: set[tuple[str, int]] = set()
    for match in _FRAME.finditer(_searchable(failure)):
        key = (match["path"], int(match["line"]))
        if key in seen:
            continue
        seen.add(key)
        frames.append(
            Frame(
                path=match["path"],
                line=int(match["line"]),
                function=match["function"],
                origin=frame_origin(match["path"], failure.step_types_dir),
            )
        )
    return tuple(reversed(frames))


def locate_settings(
    plugin_dir: Path | None, keys: Sequence[str], files: Sequence[str] = ("syntara-plugin.yaml",)
) -> list[str]:
    """Find ``key:`` lines in the plugin's settings file(s), as ``file:line`` strings."""
    if plugin_dir is None or not plugin_dir.is_dir():
        return []
    locations = []
    for name in files:
        path = plugin_dir / name
        if not path.is_file():
            continue
        for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = text.strip()
            for key in keys:
                if stripped == f"{key}:" or stripped.startswith(f"{key}: "):
                    locations.append(f"{plugin_dir.name}/{path.name}:{number} ({key})")
    return locations


def _split_keys(raw: str) -> list[str]:
    return [key.strip().strip("'\"") for key in raw.split(",") if key.strip()]


# Rules -----------------------------------------------------------------------

Rule = Callable[[Failure, tuple[Frame, ...]], Classification | None]


def _missing_tool(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    match = re.search(r"required tool '(?P<tool>[^']+)' was not found on PATH", failure.note)
    if match is None:
        return None
    return Classification(
        category="infra",
        rule_id="IN006",
        rule_name="missing-tool",
        confidence="deterministic",
        what=f"The tool '{match['tool']}' is not installed on the machine running the check.",
        evidence=(failure.note,),
        cause="The check needs uv, git, make, jq, and podman on PATH.",
        actions=(f"Install {match['tool']} and run the check again.",),
    )


def _fixture_layout(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    if failure.stage != "preflight":
        return None
    return Classification(
        category="fixture-drift",
        rule_id="FD001",
        rule_name="fixture-layout",
        confidence="deterministic",
        what=f"The pinned step-types checkout is missing what this check needs: {failure.note}",
        evidence=(failure.note,),
        cause="step-types was restructured, or the pin points at a revision this check was not "
        "written for.",
        actions=(
            "Update tests/step_types for the new step-types layout, or pin a compatible revision "
            "(STEP_TYPES_REF in .github/workflows/step-types-compat.yml).",
        ),
        # The identity of this failure for the devel-baseline comparison.
        facts={"note": failure.note},
    )


def _candidate_not_installed(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    if not failure.note.startswith("candidate not installed"):
        return None
    return Classification(
        category="fixture-drift",
        rule_id="FD003",
        rule_name="candidate-not-installed",
        confidence="deterministic",
        what="The step-types flow did not run the candidate SDK wheels: "
        + failure.note.removeprefix("candidate not installed: "),
        evidence=(failure.note,),
        cause="step-types changed how it installs or invokes the SDK, or something re-synced "
        "the environment, so the wheel injection no longer takes effect. Testing would "
        "exercise the published SDK, not this pull request.",
        actions=("Update the injection in tests/step_types/driver.py for the new layout.",),
        # The identity of this failure for the devel-baseline comparison.
        facts={"reason": failure.note.removeprefix("candidate not installed: ")},
    )


def _candidate_wheels(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    if failure.stage != "environment" or not failure.note.startswith("candidate wheels"):
        return None
    return Classification(
        category="sdk-regression",
        rule_id="SR002",
        rule_name="candidate-wheels",
        confidence="deterministic",
        what=failure.note,
        evidence=(failure.note,),
        cause="The SDK build produced a missing or inconsistent wheel set (the SDK wheel pins "
        "an exact contracts version that the built contracts wheel must match).",
        actions=("Fix the package versions or build configuration in this pull request.",),
        # The identity of this failure for the devel-baseline comparison.
        facts={"note": failure.note},
    )


def _fixture_plugin_unknown(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    text = failure.text
    if "Set PLUGIN to one of" in text:
        evidence = _matching_lines(text, "Set PLUGIN to one of")
    elif "assert_plugin_archive.py: error" in text and "invalid choice" in text:
        evidence = _matching_lines(text, "invalid choice")
    else:
        return None
    return Classification(
        category="fixture-drift",
        rule_id="FD002",
        rule_name="fixture-plugin-unknown",
        confidence="deterministic",
        what=f"step-types' own make targets or scripts do not accept the plugin "
        f"'{failure.plugin}'.",
        evidence=evidence,
        cause="The plugin is not registered in step-types' Makefile (SDK_PLUGINS) or in its "
        "archive assertion script.",
        actions=("Ask the step-types maintainers to register the plugin, then bump the pin.",),
        # The identity of this failure for the devel-baseline comparison.
        facts={"line": evidence[0]} if evidence else {},
    )


def _registry(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    if failure.stage != "registry":
        return None
    tail = [line.strip() for line in failure.stderr.splitlines() if line.strip()][-2:]
    return Classification(
        category="infra",
        rule_id="IN001",
        rule_name="registry-unavailable",
        confidence="deterministic",
        what="The local OCI registry on localhost:5000 could not be started or did not become "
        "healthy.",
        evidence=tuple(dict.fromkeys([failure.note, *tail])) if failure.note else tuple(tail),
        cause="Container engine or port problem on the runner (for example, port 5000 already "
        "taken by something that is not a registry); unrelated to the SDK change.",
        actions=("Re-run the failed job; locally, free port 5000 or stop the process using it.",),
    )


def _timeout(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    if not failure.timed_out:
        return None
    return Classification(
        category="infra",
        rule_id="IN002",
        rule_name="timeout",
        confidence="inferred",
        what=f"The {failure.stage} command exceeded its time limit and was stopped.",
        evidence=(failure.note,) if failure.note else (),
        cause="Usually a slow or stuck runner; a hang introduced by the change is also possible.",
        actions=("Re-run the failed job; if it times out again, reproduce locally.",),
    )


def _infra_signature(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    text = _searchable(failure)
    for signature in _INFRA_SIGNATURES:
        if signature in text:
            return Classification(
                category="infra",
                rule_id="IN003",
                rule_name="network-or-engine",
                confidence="deterministic",
                what=f"The {failure.stage} stage hit a network or container-engine failure.",
                evidence=_matching_lines(text, signature),
                cause="Transient runner, network, or registry problem; unrelated to the SDK "
                "change.",
                actions=("Re-run the failed job.",),
                facts={"signature": signature},
            )
    return None


def _resolution(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    if failure.stage != "environment":
        return None
    text = failure.text
    if not any(signature in text for signature in _RESOLUTION_SIGNATURES):
        return None
    return Classification(
        category="contract",
        rule_id="CT002",
        rule_name="dependency-resolution",
        confidence="deterministic",
        what="uv could not resolve the step-types environment with the candidate SDK wheels.",
        evidence=_matching_lines(text, _RESOLUTION_DETAIL, limit=8),
        cause="The candidate changed its dependencies or supported Python versions in a way "
        "that step-types' constraints reject.",
        actions=(
            INTENTIONAL,
            "Not intentional? Restore the previous dependency or Python requirements.",
        ),
    )


def _unsupported_setting(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    match = _UNSUPPORTED.search(_searchable(failure))
    if match is None:
        return None
    label = match["label"].strip()
    keys = _split_keys(match["keys"])
    locations = locate_settings(failure.plugin_dir, keys)
    where = f" in {', '.join(locations)}" if locations else ""
    facts: dict[str, Any] = {"label": label, "keys": keys, "locations": locations}
    if label == "build" and "workload" in keys:
        facts["transform"] = "workload-to-workloads"
    return Classification(
        category="misconfiguration",
        rule_id="MC001",
        rule_name="unsupported-setting",
        confidence="deterministic",
        what=f"The candidate SDK rejects the setting(s) {', '.join(keys)} under "
        f"'{label}'{where}.",
        evidence=(match.group(0).strip(),),
        cause="This pull request removed or renamed a plugin settings key that step-types "
        "still uses.",
        actions=(INTENTIONAL, "Not intentional? Restore the setting."),
        facts=facts,
    )


def _schema_violation(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    match = _SCHEMA.search(_searchable(failure))
    if match is None:
        return None
    return Classification(
        category="misconfiguration",
        rule_id="MC002",
        rule_name="settings-schema",
        confidence="deterministic",
        what=f"The plugin settings no longer match the candidate SDK's schema: {match['detail']}",
        evidence=(match.group(0).strip(),),
        cause="This pull request changed the plugin settings schema in a way step-types' "
        "settings do not satisfy.",
        actions=(INTENTIONAL, "Not intentional? Restore schema compatibility."),
        # The identity of this failure for the devel-baseline comparison.
        facts={"detail": match["detail"].strip()},
    )


def _required_setting(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    match = _REQUIRES.search(_searchable(failure))
    if match is None:
        return None
    return Classification(
        category="misconfiguration",
        rule_id="MC003",
        rule_name="required-setting",
        confidence="deterministic",
        what=f"The candidate SDK now requires {match['keys'].strip()} under "
    f"'{match['label'].strip()}'.",
        evidence=(match.group(0).strip(),),
        cause="This pull request made a plugin setting mandatory that step-types does not set.",
        actions=(INTENTIONAL, "Not intentional? Keep the setting optional or give it a default."),
        # The identity of this failure for the devel-baseline comparison.
        facts={"label": match["label"].strip(), "keys": _split_keys(match["keys"])},
    )


def _cli_usage(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    match = _CLI_USAGE.search(failure.text)
    if match is None:
        return None
    return Classification(
        category="contract",
        rule_id="CT005",
        rule_name="cli-arguments",
        confidence="deterministic",
        what=f"The candidate syntara-plugin CLI rejects the arguments step-types passes: "
        f"{match['detail'].strip()}.",
        evidence=(match.group(0).strip(),),
        cause="This pull request removed or renamed a CLI flag, subcommand, or choice that "
        "step-types' make targets still use.",
        actions=(INTENTIONAL, "Not intentional? Keep accepting the old arguments."),
        # The identity of this failure for the devel-baseline comparison.
        facts={"detail": match["detail"].strip()},
    )


def _cli_error_kind(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    error = _error(captured_document(failure))
    kind = str(error.get("kind", ""))
    message = str(error.get("message", ""))
    detail = [str(item) for item in error.get("detail", []) or [] if isinstance(item, str)]
    evidence = tuple([f"{kind}: {message}", *detail][:5])
    if kind in _SETTINGS_KINDS:
        return Classification(
            category="misconfiguration",
            rule_id="MC005",
            rule_name="run-settings-error",
            confidence="deterministic",
            what=f"syntara-plugin run reported {kind}: {message}",
            evidence=evidence,
            cause="The candidate SDK rejected the plugin workspace before running the step.",
            actions=(INTENTIONAL,),
            # The identity of this failure for the devel-baseline comparison.
            facts={"kind": kind, "message": message},
        )
    if kind == "InvalidInput":
        return Classification(
            category="contract",
            rule_id="CT006",
            rule_name="input-validation",
            confidence="deterministic",
            what="The candidate SDK rejects step-types' example input against the step's own "
            f"declared input schema: {message}",
            evidence=evidence,
            cause="This pull request changed how step inputs are validated, so an input that "
            "satisfied the step's schema before no longer does.",
            actions=(INTENTIONAL, "Not intentional? Restore the previous input validation."),
            # The identity of this failure for the devel-baseline comparison.
            facts={"kind": kind, "message": message},
        )
    return None


def _traceback_first(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    """Locate a crash from its traceback, in any stage, before any text signature.

    A traceback proves which side raised even when the exception is
    network-flavoured (``ConnectError`` from the SDK's own registry client looks
    exactly like an outage). When an infrastructure signature is also present,
    the analysis says so and the driver still retries once: a transient outage
    heals on the retry, a regression does not.
    """
    if not frames:
        return None
    located = _provenance(failure, frames)
    if located is None:
        return None
    document = captured_document(failure)
    detail = document.get("errorDetail") if document else None
    text = detail if isinstance(detail, str) else _searchable(failure)
    evidence = [located.evidence[0]]
    exception = _exception_line(text)
    if exception:
        evidence.append(exception)
    facts: dict[str, Any] = {}
    error = _error(document)
    code = str(error.get("code", ""))
    if code:
        evidence.append(f"error.code {code}: {error.get('message', '')}")
        facts["code"] = code
    signature = next((s for s in _INFRA_SIGNATURES if s in _searchable(failure)), None)
    cause = located.cause
    if signature:
        facts["network_signature"] = signature
        cause += (
            f" The exception is network-flavoured ({signature}): if it persists after the "
            "automatic retry, suspect the code at that frame (for example a wrong host or "
            "port), not the network."
        )
    return replace(located, evidence=tuple(evidence), facts=facts, cause=cause)


def _compiler_diagnostics(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    """Classify an ``{"ok": false, "diagnostics": [...]}`` document from the compiler."""
    document = captured_document(failure)
    if document is None or document.get("ok") is not False:
        return None
    diagnostics = [d for d in document.get("diagnostics") or [] if isinstance(d, Mapping)]
    if not diagnostics:
        return None
    evidence = []
    for item in diagnostics[:5]:
        location = item.get("location") if isinstance(item.get("location"), Mapping) else {}
        where = ":".join(str(location[k]) for k in ("file", "line") if location.get(k))
        if location.get("path") and location.get("path") != "$":
            where = f"{where} {location['path']}".strip()
        evidence.append(f"{item.get('code', '?')} {where}: {item.get('message', '')}".strip())
    codes = sorted({str(item.get("code", "?")) for item in diagnostics})
    first = diagnostics[0]
    remediation = [str(d["remediation"]) for d in diagnostics[:3] if d.get("remediation")]
    return Classification(
        category="misconfiguration",
        rule_id="MC006",
        rule_name="compiler-diagnostic",
        confidence="deterministic",
        what=f"The candidate SDK's compiler rejects the step-types plugin workspace during "
        f"{failure.stage} ({', '.join(codes)}): {first.get('message', '')}",
        evidence=tuple(evidence),
        cause="This pull request changed a compiler rule that step-types' manifests no longer "
        "satisfy (or step-types has a defect the compiler now catches).",
        actions=(
            INTENTIONAL,
            "Not intentional? Restore the previous compiler behaviour for "
            f"{', '.join(codes)}.",
            *(f"The SDK's remediation for step-types: {text}" for text in remediation),
        ),
        facts={"codes": codes},
    )


_TEXT_DIAGNOSTIC = re.compile(r"^error: (?P<code>[A-Z][A-Z0-9_]{2,}): (?P<message>.+)$", re.MULTILINE)
_DIAGNOSTIC_HINTS = {
    "IMAGE_BINDING_MISSING": "Custom-runner images now come from a RuntimeBinding "
    "(`build.runtimeBindings` in syntara-plugin.yaml) or an explicit `--image-binding`; see "
    "docs/runtime-bindings.md in the SDK repository.",
}


def _compiler_text_diagnostic(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    """Classify ``error: CODE: message`` compiler diagnostics printed as text on stderr."""
    matches = list(_TEXT_DIAGNOSTIC.finditer(failure.stderr))
    if not matches:
        return None
    codes = sorted({match["code"] for match in matches})
    hints = [_DIAGNOSTIC_HINTS[code] for code in codes if code in _DIAGNOSTIC_HINTS]
    return Classification(
        category="misconfiguration",
        rule_id="MC006",
        rule_name="compiler-diagnostic",
        confidence="deterministic",
        what=f"The candidate SDK rejects the step-types plugin workspace during {failure.stage} "
        f"({', '.join(codes)}): {matches[0]['message']}",
        evidence=tuple(dict.fromkeys(match.group(0).strip() for match in matches))[:5],
        cause="This pull request changed a compiler or build rule that step-types' workspace no "
        "longer satisfies.",
        actions=(
            INTENTIONAL,
            f"Not intentional? Restore the previous behaviour for {', '.join(codes)}.",
            *hints,
        ),
        facts={"codes": codes},
    )


def _run_document_rule(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    """Classify a failed run from the run document the CLI printed."""
    if failure.stage != "run":
        return None
    document = captured_document(failure)
    if document is None or document.get("ok") is not False:
        return None
    error = _error(document)
    code = str(error.get("code", ""))
    message = str(error.get("message", ""))
    reported = f"error.code {code}: {message}" if code else ""
    if not code or error.get("kind"):
        return None
    if code in _TRANSIENT_CODES:
        return Classification(
            category="infra",
            rule_id="IN004",
            rule_name="upstream-transient",
            confidence="deterministic",
            what=f"The step reported a transient dependency failure ({code}): {message}",
            evidence=(reported,),
            cause="A service the step's example input depends on was unavailable, slow, or "
            "rate limiting; the step handled it as designed.",
            actions=("Re-run the failed job.",),
            facts={"code": code, "retryable": error.get("retryable")},
        )
    if code in _BOUNDARY_CODES:
        return Classification(
            category="contract",
            rule_id="CT007",
            rule_name="runtime-boundary",
            confidence="deterministic",
            what=f"The SDK runtime reported {code}: {message}",
            evidence=(reported,),
            cause="This pull request changed a runtime boundary the step depends on.",
            actions=(INTENTIONAL,),
            # The identity of this failure for the devel-baseline comparison.
            facts={"code": code, "message": message},
        )
    if code in _INPUT_CODES:
        # The runtime validates the step's inputs before the step runs, so this
        # is the SDK rejecting step-types' example input, not the step failing.
        return Classification(
            category="contract",
            rule_id="CT006",
            rule_name="input-validation",
            confidence="deterministic",
            what="The candidate SDK's runtime rejected step-types' example input against "
            f"the step's declared schema: {message}",
            evidence=(reported,),
            cause="This pull request changed how step inputs are validated at runtime.",
            actions=(INTENTIONAL, "Not intentional? Restore the previous validation."),
            # The identity of this failure for the devel-baseline comparison.
            facts={"code": code, "message": message},
        )
    if code in _STEP_REPORTED_CODES:
        return Classification(
            category="implementation",
            rule_id="IM003",
            rule_name="step-reported-failure",
            confidence="deterministic",
            what=f"The step itself reported {code} for its example input: {message}",
            evidence=(reported,),
            cause="The step ran and rejected its own example input; its behaviour under the "
            "candidate SDK differs from before (or the pull request changed what the step "
            "receives).",
            actions=(
                INTENTIONAL,
                "Not intentional? Compare the diagnostic run output below with the step's "
                "example input and expected output.",
            ),
            # The identity of this failure for the devel-baseline comparison.
            facts={"code": code, "message": message},
        )
    # OUTCOME_UNKNOWN, or a code the SDK added without a mapping: without a
    # traceback the report does not establish an origin, so stay uncertain.
    actions = [_REPRODUCE.format(plugin=failure.plugin)]
    if code not in _UNDETERMINED_CODES:
        actions.append(
            "This looks like a new SDK FailureCode; add it to the run-code sets in "
            "classifier.py."
        )
    return Classification(
        category="unknown",
        rule_id="UN000",
        rule_name="unclassified",
        confidence="unclassified",
        what=f"The run failed with error.code {code} and no traceback to locate the "
        f"failure: {message}",
        evidence=(reported,),
        cause="Not determined; read the full output below.",
        actions=tuple(actions),
        facts={"code": code},
    )


def _runtime_abi(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    text = _searchable(failure)
    if _ABI_SIGNATURE in text:
        evidence = _matching_lines(text, _ABI_SIGNATURE)
        return Classification(
            category="contract",
            rule_id="CT001",
            rule_name="runtime-abi",
            confidence="deterministic",
            what="The step runtime rejected the requested runtime ABI.",
            evidence=evidence,
            cause="This pull request changed the container runtime ABI or protocol.",
            actions=(INTENTIONAL,),
            # The identity of this failure for the devel-baseline comparison.
            facts={"line": evidence[0]} if evidence else {},
        )
    match = _GRPC_SIGNATURE.search(text)
    if match is not None:
        return Classification(
            category="contract",
            rule_id="CT004",
            rule_name="grpc-protocol",
            confidence="inferred",
            what="A gRPC call between the runner and the step runtime failed.",
            evidence=_matching_lines(text, match.group(0)),
            cause="Often a container protocol change in this pull request; a crash on either "
            "side of the call produces the same error.",
            actions=(INTENTIONAL, "Check the traceback analysis below for the failing side."),
        )
    return None


def _expectation_false(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    if not _jq_false(failure):
        return None
    if failure.stage == "run":
        document = captured_document(failure)
        if document is None or document.get("ok") is not True:
            return None
        output = document.get("output")
        if not isinstance(output, Mapping):
            return Classification(
                category="contract",
                rule_id="CT003",
                rule_name="cli-output-shape",
                confidence="deterministic",
                what="syntara-plugin run reported success but its JSON has no .output object, "
                "which step-types' expectation reads.",
                evidence=(f"run --json keys: {', '.join(sorted(document))}",),
                cause="This pull request changed the `run --json` report shape.",
                actions=(INTENTIONAL, "Not intentional? Restore the .output field."),
            )
        shown = {
            key: output[key]
            for key in ("status_code", "return_code", "stdout", "stdout_json")
            if key in output
        }
        return Classification(
            category="implementation",
            rule_id="IM001",
            rule_name="output-expectation",
            confidence="deterministic",
            what="The step ran, but step-types' expectation for its output evaluated to false.",
            evidence=("jq -e filter in step-types' sdk-plugin-run target printed: false",)
            + ((f"diagnostic run .output: {json.dumps(shown)}",) if shown else ()),
            cause="The step's behaviour changed under the candidate SDK, or this pull request "
            "changed the run report fields the expectation reads.",
            actions=(
                INTENTIONAL,
                "Not intentional? Compare the diagnostic run output below with the jq filter in "
                "step-types' Makefile (sdk-plugin-run).",
            ),
        )
    document = captured_document(failure)
    if document is None or document.get("ok") is False:
        # Without the document (or with a failure report the other rules did not
        # recognise) a jq "false" proves nothing about the output's shape.
        return None
    return Classification(
        category="contract",
        rule_id="CT003",
        rule_name="cli-output-shape",
        confidence="deterministic",
        what=f"A jq expectation on the candidate CLI's JSON output evaluated to false during "
        f"{failure.stage}, although the command reported no failure.",
        evidence=(
            "jq -e filter in step-types' make target printed: false",
            f"captured --json keys: {', '.join(sorted(document)) or '(none)'}",
        ),
        cause="This pull request changed the `--json` output fields step-types' make targets "
        "check.",
        actions=(INTENTIONAL, "Not intentional? Restore the previous JSON fields."),
    )


def _rejected_workspace(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    if failure.stage != "validate":
        return None
    lines = _matching_lines(failure.stderr, "error:")
    if not any(line.startswith("error:") for line in lines):
        return None
    return Classification(
        category="misconfiguration",
        rule_id="MC004",
        rule_name="workspace-rejected",
        confidence="inferred",
        what="The candidate SDK rejected the step-types plugin workspace during validation.",
        evidence=lines,
        cause="Most likely a validation rule changed in this pull request; a validation bug "
        "produces the same symptom.",
        actions=(INTENTIONAL,),
    )


def _provenance(failure: Failure, frames: tuple[Frame, ...]) -> Classification | None:
    for frame in frames:
        location = f"{frame.path}:{frame.line} in {frame.function}"
        if frame.origin == "step-types":
            return Classification(
                category="implementation",
                rule_id="IM002",
                rule_name="step-types-frame",
                confidence="inferred",
                what=f"The failure is raised inside step-types code ({location}).",
                evidence=(location,),
                cause="The step's code failed under the candidate SDK; this pull request likely "
                "changed behaviour that code depends on.",
                actions=(
                    INTENTIONAL,
                    "Use the traceback analysis below to see which SDK call the step-types code "
                    "was making.",
                ),
                frames=frames,
            )
        if frame.origin == "sdk":
            return Classification(
                category="sdk-regression",
                rule_id="SR001",
                rule_name="sdk-frame",
                confidence="inferred",
                what=f"The failure is raised inside the candidate SDK ({location}).",
                evidence=(location,),
                cause="The candidate SDK code crashed while serving step-types.",
                actions=(
                    "Fix the regression in this pull request; the traceback analysis below "
                    "points at the SDK frame.",
                ),
                frames=frames,
            )
    return None


RULES: tuple[Rule, ...] = (
    _missing_tool,
    _fixture_layout,
    _candidate_not_installed,
    _candidate_wheels,
    _fixture_plugin_unknown,
    _timeout,
    _registry,
    _traceback_first,
    _infra_signature,
    _resolution,
    _unsupported_setting,
    _schema_violation,
    _required_setting,
    _cli_usage,
    _cli_error_kind,
    _compiler_diagnostics,
    _compiler_text_diagnostic,
    _run_document_rule,
    _runtime_abi,
    _expectation_false,
    _rejected_workspace,
)


def _unclassified(failure: Failure, frames: tuple[Frame, ...], note: str = "") -> Classification:
    tail = [line for line in failure.stderr.strip().splitlines() if line.strip()][-3:]
    evidence = tuple(tail) or ((failure.note,) if failure.note else ())
    return Classification(
        category="unknown",
        rule_id="UN000",
        rule_name="unclassified",
        confidence="unclassified",
        what=f"The {failure.stage} stage failed (exit code {failure.exit_code}) with no known "
        f"signature.{note}",
        evidence=evidence,
        cause="Not determined; read the full output below.",
        actions=(_REPRODUCE.format(plugin=failure.plugin),),
        frames=frames,
    )


def classify(failure: Failure) -> Classification:
    """Return the analysis for ``failure``; never raises."""
    try:
        frames = parse_frames(failure)
        for rule in RULES:
            result = rule(failure, frames)
            if result is not None:
                if not result.frames and frames:
                    result = replace(result, frames=frames)
                return result
        return _unclassified(failure, frames)
    except Exception as error:  # A classifier bug must never decide the verdict.
        return _unclassified(failure, (), f" (the classifier itself failed: {error!r})")


NOT_VERIFIED = frozenset({"infra", "fixture-drift", "unknown"})


def outcome_word(category: str) -> str:
    """How to describe a failed step type: a compatibility verdict or a check that could not run."""
    return "not verified" if category in NOT_VERIFIED else "incompatible"


def reproduce_hint(plugin: str) -> str:
    return _REPRODUCE.format(plugin=plugin)
