"""Tests for the devel-baseline comparison (inherited vs new failures)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import baseline
import classifier
from baseline import absence_reason, inherited, load

PIN = "3991e986f92f4b1b00a76e21902038215915bd19"
OTHER_PIN = "1111111111111111111111111111111111111111"
FACTS = {"label": "build", "keys": ["workload"]}


def report(
    *,
    result: str = "fail",
    stage: str = "validate",
    rule: str = "MC001",
    category: str = "misconfiguration",
    facts: dict[str, Any] | None = None,
    frames: list[dict[str, Any]] | None = None,
    adapter: dict[str, Any] | None = None,
    pin: str = PIN,
    schema: int = 1,
    confidence: str = "deterministic",
) -> dict[str, Any]:
    return {
        "schema": schema,
        "plugin": "http-request",
        "result": result,
        "pin": {"sha": pin, "date": "2026-11-12T10:00:00+00:00", "age_days": 1},
        "failure": None
        if result == "pass"
        else {
            "stage": stage,
            "index": 3,
            "category": category,
            "rule_id": rule,
            "confidence": confidence,
            "facts": FACTS if facts is None else facts,
            "frames": frames if frames is not None else [],
        },
        "adapter": adapter,
    }


def frame(path: str = "plugins/http-request/step.py", function: str = "main", origin: str = "step-types") -> dict[str, Any]:
    return {"path": path, "line": 12, "function": function, "origin": origin}


# The policy table -------------------------------------------------------------


def test_policy_covers_every_classifier_rule_exactly_once() -> None:
    source = Path(classifier.__file__).read_text(encoding="utf-8")
    produced = set(re.findall(r'rule_id="([A-Z]{2}\d{3})"', source))
    tables = [baseline.NEVER, baseline.FACTS.keys(), baseline.ALTERNATIVES.keys(), baseline.FRAMES.keys()]
    covered = set().union(*tables)
    assert produced == covered, (
        f"every classifier rule needs exactly one baseline policy entry; "
        f"missing from the policy: {sorted(produced - covered)}; "
        f"in the policy but not produced: {sorted(covered - produced)}"
    )
    for first in range(len(tables)):
        for second in range(first + 1, len(tables)):
            overlap = set(tables[first]) & set(tables[second])
            assert not overlap, f"rules listed in two policy tables: {sorted(overlap)}"


def test_infra_and_unknown_are_never_inheritable() -> None:
    for rule in ("IN001", "IN002", "IN003", "IN004", "IN006", "UN000"):
        assert rule in baseline.NEVER
        assert inherited(report(rule=rule), report(rule=rule)) is None


def test_rules_without_stable_identity_are_never_inheritable() -> None:
    for rule in ("CT002", "CT003", "IM001"):
        assert rule in baseline.NEVER
        assert inherited(report(rule=rule), report(rule=rule)) is None


# Matching ---------------------------------------------------------------------


def test_identical_failure_is_inherited() -> None:
    assert inherited(report(), report()) is not None


def test_smaller_broken_set_is_inherited_as_an_improvement() -> None:
    decision = inherited(report(facts={"label": "build", "keys": ["workload"]}), report(
        facts={"label": "build", "keys": ["workload", "publish"]}
    ))
    assert decision is not None and decision.improved


def test_extra_broken_field_is_new() -> None:
    decision = inherited(report(facts={"label": "build", "keys": ["workload", "publish"]}), report(
        facts={"label": "build", "keys": ["workload"]}
    ))
    assert decision is None


def test_different_broken_field_is_new() -> None:
    decision = inherited(report(facts={"label": "build", "keys": ["publish"]}), report(
        facts={"label": "build", "keys": ["workload"]}
    ))
    assert decision is None


def test_derived_facts_do_not_participate() -> None:
    # Fixing `workload` drops the derived `transform` and shrinks `locations`;
    # the comparison must still see an improvement, not a mismatch.
    decision = inherited(
        report(facts={"label": "build", "keys": ["x"], "locations": ["a"], "transform": "t"}),
        report(facts={"label": "build", "keys": ["x", "workload"], "locations": ["a", "b"], "transform": "t"}),
    )
    assert decision is not None and decision.improved


def test_missing_fact_on_either_side_is_new() -> None:
    assert inherited(report(facts={}), report()) is None
    assert inherited(report(), report(facts={})) is None


def test_different_stage_or_rule_is_new() -> None:
    assert inherited(report(stage="run"), report(stage="validate")) is None
    assert inherited(report(rule="MC002", facts={"detail": "d"}), report(rule="MC003", facts={"label": "b", "keys": []})) is None


def test_pin_must_match_exactly() -> None:
    assert inherited(report(), report(pin=OTHER_PIN)) is None


def test_unknown_pin_sentinel_never_matches() -> None:
    assert inherited(report(pin="unknown"), report(pin="unknown")) is None


def test_short_pin_never_matches() -> None:
    assert inherited(report(pin="abc123"), report(pin="abc123")) is None


def test_equal_scalar_facts_match_and_different_do_not() -> None:
    same = report(rule="MC005", facts={"kind": "SettingsError", "message": "publish requires x"})
    assert inherited(same, report(rule="MC005", facts=same["failure"]["facts"])) is not None
    assert inherited(same, report(rule="MC005", facts={"kind": "SettingsError", "message": "other"})) is None
    assert inherited(same, report(rule="MC005", facts={"kind": "CompilationError", "message": "publish requires x"})) is None


def test_ct006_matches_on_either_producers_identity() -> None:
    # The CLI producer reports error.kind, the runtime producer error.code;
    # either pair identifies the failure, but a kind never matches a code.
    kind = report(rule="CT006", stage="run", facts={"kind": "InvalidInput", "message": "m"})
    code = report(rule="CT006", stage="run", facts={"code": "INVALID_INPUT", "message": "m"})
    assert inherited(kind, report(rule="CT006", stage="run", facts={"kind": "InvalidInput", "message": "m"})) is not None
    assert inherited(code, report(rule="CT006", stage="run", facts={"code": "INVALID_INPUT", "message": "m"})) is not None
    assert inherited(kind, code) is None
    assert inherited(
        kind, report(rule="CT006", stage="run", facts={"kind": "InvalidInput", "message": "other"})
    ) is None


def test_ct006_adapter_residual_matches_by_producer() -> None:
    def residual(facts: dict[str, Any]) -> dict[str, Any]:
        return {"stage": "run", "rule_id": "CT006", "facts": facts}

    ours = report(rule="MC001", adapter={"outcome": "failed", "residual": residual({"code": "INVALID_INPUT", "message": "m"})})
    same = report(rule="MC001", adapter={"outcome": "failed", "residual": residual({"code": "INVALID_INPUT", "message": "m"})})
    other = report(rule="MC001", adapter={"outcome": "failed", "residual": residual({"kind": "InvalidInput", "message": "m"})})
    assert inherited(ours, same) is not None
    assert inherited(ours, other) is None


def test_facts_with_unexpected_types_never_match_and_never_raise() -> None:
    weird = {"keys": [{"nested": True}], "label": {"a": 1}}
    assert inherited(report(facts=weird), report(facts=weird)) is None


# Frames -----------------------------------------------------------------------


def test_frame_rules_match_on_path_and_function() -> None:
    ours = report(rule="IM002", facts={}, frames=[frame()])
    theirs = report(rule="IM002", facts={}, frames=[frame()])
    assert inherited(ours, theirs) is not None


def test_frame_rules_ignore_the_line_number() -> None:
    shifted = frame()
    shifted["line"] = 999
    assert inherited(report(rule="IM002", facts={}, frames=[frame()]), report(rule="IM002", facts={}, frames=[shifted])) is not None


def test_frame_rules_require_the_right_origin() -> None:
    sdk_frames = [frame(origin="sdk")]
    assert inherited(report(rule="IM002", facts={}, frames=sdk_frames), report(rule="IM002", facts={}, frames=sdk_frames)) is None
    assert inherited(report(rule="SR001", facts={}, frames=sdk_frames), report(rule="SR001", facts={}, frames=sdk_frames)) is not None


def test_frame_rules_without_frames_are_new() -> None:
    assert inherited(report(rule="IM002", facts={}), report(rule="IM002", facts={})) is None


def test_frame_rules_with_malformed_frames_never_raise() -> None:
    broken = [{"path": 12}]
    assert inherited(report(rule="IM002", facts={}, frames=broken), report(rule="IM002", facts={}, frames=broken)) is None


# The adapter ------------------------------------------------------------------


def residual(rule: str = "MC001", facts: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"stage": "validate", "rule_id": rule, "facts": facts if facts is not None else FACTS}


def test_adapter_getting_worse_makes_the_failure_new() -> None:
    # The visible failure matches devel's, but the migrated re-run now fails.
    decision = inherited(
        report(adapter={"outcome": "failed", "residual": residual(facts={"label": "publish", "keys": ["tag"]})}),
        report(adapter={"outcome": "passed"}),
    )
    assert decision is None


def test_adapter_same_residual_still_inherits() -> None:
    decision = inherited(
        report(adapter={"outcome": "failed", "residual": residual()}),
        report(adapter={"outcome": "failed", "residual": residual()}),
    )
    assert decision is not None


def test_adapter_different_residual_is_new() -> None:
    decision = inherited(
        report(adapter={"outcome": "failed", "residual": residual(facts={"label": "publish", "keys": ["tag"]})}),
        report(adapter={"outcome": "failed", "residual": residual()}),
    )
    assert decision is None


def test_adapter_residual_in_a_different_stage_is_new() -> None:
    other = residual()
    other["stage"] = "run"
    decision = inherited(
        report(adapter={"outcome": "failed", "residual": other}),
        report(adapter={"outcome": "failed", "residual": residual()}),
    )
    assert decision is None


def test_passed_or_absent_adapter_never_blocks_inheritance() -> None:
    assert inherited(report(adapter={"outcome": "passed"}), report()) is not None
    assert inherited(report(), report(adapter={"outcome": "passed"})) is not None


# Loading and absence reasons ---------------------------------------------------


def write(directory: Path, name: str, document: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(document), encoding="utf-8")


def test_load_keeps_only_schema_one_documents(tmp_path: Path) -> None:
    write(tmp_path, "http-request.json", report())
    write(tmp_path, "script-bash.json", report(schema=2))
    write(tmp_path, "script-python.json", "not json at all")
    reports, _ = load(tmp_path, ["http-request", "script-bash", "script-python", "missing"])
    assert reports["http-request"] is not None
    assert reports["script-bash"] is None
    assert reports["script-python"] is None
    assert reports["missing"] is None


def test_load_reads_meta_json(tmp_path: Path) -> None:
    write(tmp_path, "meta.json", {"base_sha": PIN, "run_id": "42"})
    reports, meta = load(tmp_path, [])
    assert reports == {}
    assert meta == {"base_sha": PIN, "run_id": "42"}


def test_load_survives_oversized_files(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "http-request.json").write_text("x" * (2 * 1024 * 1024 + 1), encoding="utf-8")
    reports, meta = load(tmp_path, ["http-request"])
    assert reports["http-request"] is None and meta == {}


def test_absence_reasons() -> None:
    assert "did not complete" in absence_reason({})
    pending = absence_reason({"base_sha": PIN, "run_id": ""})
    assert "no completed devel run" in pending and PIN[:12] in pending
    expired = absence_reason({"base_sha": PIN, "run_id": "42"})
    assert "could not be downloaded" in expired and "42" in expired


def test_inherited_never_raises_on_garbage() -> None:
    assert inherited(None, None) is None
    assert inherited(report(), "garbage") is None
    assert inherited("garbage", report()) is None
    assert inherited(report(facts={"keys": None}), report(facts={"keys": None})) is None
