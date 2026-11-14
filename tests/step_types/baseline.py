"""Decide whether a pull request's failure is already on devel.

The gate (``report.py``) compares the pull request's leg reports with the
baseline: the devel push run of this same workflow for the pull request's
base commit. A failure devel already carries, with the same identity, is
*inherited*: reported, but not failing. Anything else is new.

Identity is per rule. Most rules compare named facts (lists by subset, so an
improvement inherits); rules that locate a crash compare the traceback frame
they were located on; rules without a stable identity, all infra rules, and
unknown failures are never inherited. A rule missing from the policy is
never inherited: the allowlist is the whole policy, and a sync test keeps it
aligned with the classifier.

The baseline is untrusted input: nothing here may raise on it, and every
doubt means "new".
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Rules whose failures are never inherited. Infra is retried, not inherited
# (and a devel-side infra flake never blocks a PR anyway: a PR leg is only
# red when the PR's own run failed). Unknown is where unmapped regressions
# land. The rest have no identity stable enough to compare.
NEVER: frozenset[str] = frozenset(
    {
        "IN001", "IN002", "IN003", "IN004", "IN006",  # infra
        "UN000",  # unknown still fails, as the docs promise
        "CT002",  # dependency resolution: evidence carries runner paths
        "CT003",  # cli output shape: present keys say nothing about the miss
        "IM001",  # output expectation: values wobble between runs
    }
)

# Facts compared for inheritable rules: fact name -> "eq" | "subset".
# "subset" means the pull request's list must be contained in devel's (a
# strictly smaller list is an improvement). Facts not named here, such as
# MC001's derived "locations" and "transform", do not participate.
FACTS: dict[str, dict[str, str]] = {
    "MC001": {"label": "eq", "keys": "subset"},
    "MC002": {"detail": "eq"},
    "MC003": {"label": "eq", "keys": "subset"},
    "MC005": {"kind": "eq", "message": "eq"},
    "MC006": {"codes": "subset"},
    "CT001": {"line": "eq"},
    "CT005": {"detail": "eq"},
    "CT007": {"code": "eq", "message": "eq"},
    "IM003": {"code": "eq", "message": "eq"},
    "SR002": {"note": "eq"},
    "FD001": {"note": "eq"},
    "FD002": {"line": "eq"},
    "FD003": {"reason": "eq"},
}

# Rules produced by two code paths that carry different identity facts:
# the failure matches when every fact of one group is present and equal on
# both sides. CT006's CLI producer reports error.kind, its runtime producer
# error.code; either pair identifies the failure, and a kind on one side
# with a code on the other never matches (fail closed).
ALTERNATIVES: dict[str, tuple[tuple[str, ...], ...]] = {
    "CT006": (("kind", "message"), ("code", "message")),
}

# Rules identified by the traceback frame they were located on: rule ->
# the origin of the frame that identifies them ("step-types", "sdk", or
# "any" for the first frame). Line numbers are ignored: the pull request
# may shift them without changing the failure.
FRAMES: dict[str, str] = {
    "IM002": "step-types",
    "SR001": "sdk",
    "MC004": "any",
    "CT004": "any",
}

_PIN = re.compile(r"[0-9a-f]{40}")
_MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Inherited:
    """A failure devel already carries."""

    improved: bool  # the pull request breaks strictly less than devel


def _pin_of(report: Any) -> str | None:
    pin = report.get("pin") if isinstance(report, dict) else None
    sha = pin.get("sha") if isinstance(pin, dict) else None
    if not isinstance(sha, str) or not _PIN.fullmatch(sha):
        return None  # includes the "unknown" sentinel and short hashes
    return sha


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _facts_match(rule: str, pr: Any, base: Any) -> tuple[bool, bool]:
    """Whether the named facts match, and whether the pull request improves."""
    pr_facts = pr.get("facts") if isinstance(pr, dict) else None
    base_facts = base.get("facts") if isinstance(base, dict) else None
    if not isinstance(pr_facts, dict) or not isinstance(base_facts, dict):
        return False, False
    improved = False
    for name, how in FACTS[rule].items():
        if name not in pr_facts or name not in base_facts:
            return False, False
        ours, theirs = pr_facts[name], base_facts[name]
        if how == "subset":
            if not (isinstance(ours, list) and isinstance(theirs, list)):
                return False, False
            if not all(_scalar(item) for item in [*ours, *theirs]):
                return False, False
            if not set(ours) <= set(theirs):
                return False, False
            improved = improved or set(ours) < set(theirs)
        elif not (_scalar(ours) and _scalar(theirs) and ours == theirs):
            return False, False
    return True, improved


def _fact_equal(pr: Any, base: Any, name: str) -> bool:
    def value(side: Any) -> Any:
        facts = side.get("facts") if isinstance(side, dict) else None
        return facts.get(name) if isinstance(facts, dict) else None

    ours, theirs = value(pr), value(base)
    return _scalar(ours) and _scalar(theirs) and ours == theirs


def _frame_identity(rule: str, failure: Any) -> tuple[str, str] | None:
    frames = failure.get("frames") if isinstance(failure, dict) else None
    if not isinstance(frames, list):
        return None
    wanted = FRAMES[rule]
    for frame in frames:
        if not isinstance(frame, dict):
            return None
        if wanted != "any" and frame.get("origin") != wanted:
            continue
        path = frame.get("path")
        function = frame.get("function")
        if not isinstance(path, str) or not isinstance(function, str):
            return None
        return path, function
    return None


def _same_rule(pr: Any, base: Any) -> tuple[bool, bool]:
    """Whether two failure dicts are the same failure, by the rule's policy."""
    rule = pr.get("rule_id") if isinstance(pr, dict) else None
    if not isinstance(rule, str) or rule != (base.get("rule_id") if isinstance(base, dict) else None):
        return False, False
    if rule in NEVER:
        return False, False
    if rule in ALTERNATIVES:
        same = any(
            all(_fact_equal(pr, base, name) for name in group)
            for group in ALTERNATIVES[rule]
        )
        return same, False
    if rule in FACTS:
        return _facts_match(rule, pr, base)
    if rule in FRAMES:
        ours = _frame_identity(rule, pr)
        return ours is not None and ours == _frame_identity(rule, base), False
    return False, False  # unlisted rules are never inherited


def _adapter_worse(pr_report: Any, base_report: Any) -> bool:
    """Whether the adapter's re-run on the migrated copy got worse."""
    ours = pr_report.get("adapter") if isinstance(pr_report, dict) else None
    theirs = base_report.get("adapter") if isinstance(base_report, dict) else None
    if not isinstance(ours, dict) or ours.get("outcome") != "failed":
        return False
    if not isinstance(theirs, dict) or theirs.get("outcome") != "failed":
        return True  # devel's migration passed (or never ran); this one fails
    our_residual = ours.get("residual")
    their_residual = theirs.get("residual")
    if not isinstance(our_residual, dict) or not isinstance(their_residual, dict):
        return True  # a failed re-run without a residual cannot be compared
    if our_residual.get("stage") != their_residual.get("stage"):
        return True
    return not _same_rule(our_residual, their_residual)[0]


def inherited(pr_report: Any, base_report: Any) -> Inherited | None:
    """Whether the pull request's failing report is inherited from devel.

    ``None`` means "not inherited" (new, or nothing to compare against);
    the caller treats both the same way, and no path here raises.
    """
    if not isinstance(pr_report, dict) or not isinstance(base_report, dict):
        return None
    pr_failure = pr_report.get("failure")
    base_failure = base_report.get("failure")
    if not isinstance(pr_failure, dict) or not isinstance(base_failure, dict):
        return None
    pins = (_pin_of(pr_report), _pin_of(base_report))
    if pins[0] is None or pins[0] != pins[1]:
        return None
    if pr_failure.get("stage") != base_failure.get("stage"):
        return None
    same, improved = _same_rule(pr_failure, base_failure)
    if not same:
        return None
    if _adapter_worse(pr_report, base_report):
        return None
    return Inherited(improved=improved)


def load(directory: Path, plugins: Sequence[str]) -> tuple[dict[str, dict[str, Any] | None], dict[str, Any]]:
    """Load baseline reports and ``meta.json``, with the untrusted-input guards.

    A report is kept only when it parses, carries ``schema`` 1, and is under
    2 MiB; everything else becomes ``None`` (the plugin's failures are new).
    """
    meta: dict[str, Any] = {}
    try:
        meta_path = directory / "meta.json"
        if meta_path.stat().st_size <= _MAX_BYTES:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
    except (OSError, ValueError, RecursionError):
        meta = {}
    reports: dict[str, dict[str, Any] | None] = {}
    for plugin in plugins:
        try:
            path = directory / f"{plugin}.json"
            document = (
                json.loads(path.read_text(encoding="utf-8"))
                if path.stat().st_size <= _MAX_BYTES
                else None
            )
        except (OSError, ValueError, RecursionError):
            document = None
        reports[plugin] = (
            document if isinstance(document, dict) and document.get("schema") == 1 else None
        )
    return reports, meta


def absence_reason(meta: dict[str, Any]) -> str:
    """Why there is no baseline, for the gate's summary line."""
    if "run_id" not in meta:
        return "the baseline lookup did not complete"
    run_id = meta.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        base = meta.get("base_sha")
        where = f" for base commit {str(base)[:12]}" if isinstance(base, str) and base else ""
        return f"no completed devel run{where} yet; re-run this job once it finishes"
    return f"the baseline run's artifacts could not be downloaded (run {run_id}, expired or deleted)"
