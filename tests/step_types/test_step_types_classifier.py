"""Tests for the deterministic failure classifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from classifier import Failure, classify, frame_origin, parse_frames

MAKE_ERROR = "make[1]: *** [Makefile:71: sdk-plugin-validate] Error 4\n"


def failure(**fields: object) -> Failure:
    base = {"stage": "validate", "plugin": "http-request", "exit_code": 2}
    base.update(fields)
    return Failure(**base)  # type: ignore[arg-type]


def test_unsupported_setting_is_deterministic_misconfiguration(tmp_path: Path) -> None:
    plugin = tmp_path / "http-request"
    plugin.mkdir()
    (plugin / "syntara-plugin.yaml").write_text("build:\n  workload:\n    engine: podman\n")
    (plugin / "plugin.yaml").write_text("spec:\n  workload:\n    image: x\n")
    result = classify(
        failure(
            stderr="error: build contains unsupported setting(s): workload\n" + MAKE_ERROR,
            plugin_dir=plugin,
        )
    )
    assert (result.category, result.rule_id, result.confidence) == (
        "misconfiguration",
        "MC001",
        "deterministic",
    )
    assert result.facts["keys"] == ["workload"]
    assert result.facts["locations"] == ["http-request/syntara-plugin.yaml:2 (workload)"]
    assert result.facts["transform"] == "workload-to-workloads"
    assert result.evidence == ("build contains unsupported setting(s): workload",)


def test_unsupported_setting_inside_run_json_error_document() -> None:
    document = {
        "error": {"kind": "SettingsError", "message": "build contains unsupported setting(s): x"},
        "ok": False,
    }
    result = classify(failure(stage="run", stdout=json.dumps(document)))
    assert result.rule_id == "MC001"
    assert "transform" not in result.facts


def test_other_settings_error_kind_from_run_json() -> None:
    document = {"error": {"kind": "SettingsError", "message": "manifest not found"}, "ok": False}
    result = classify(failure(stage="run", diagnostic=document))
    assert (result.category, result.rule_id) == ("misconfiguration", "MC005")


def test_schema_violation() -> None:
    stderr = (
        "error: settings document schema validation failed: build/workloads/x: Additional "
        "properties are not allowed ('imageId' was unexpected)\n"
    )
    result = classify(failure(stderr=stderr))
    assert (result.category, result.rule_id) == ("misconfiguration", "MC002")


def test_removed_cli_flag_is_contract() -> None:
    stderr = (
        "usage: syntara-plugin [-h]\n"
        "syntara-plugin: error: unrecognized arguments: --with-workload --workload-tag\n"
        "make[1]: *** [Makefile:85: sdk-plugin-build-inspect] Error 2\n"
    )
    result = classify(failure(stage="build-inspect", stderr=stderr))
    assert (result.category, result.rule_id, result.confidence) == (
        "contract",
        "CT005",
        "deterministic",
    )
    assert "--with-workload --workload-tag" in result.what


def test_dependency_resolution_is_contract() -> None:
    stderr = (
        "  x No solution found when resolving dependencies:\n"
        "  ╰─▶ Because syntara-plugin-sdk==0.2.0 depends on Python>=3.15 ...\n"
    )
    result = classify(failure(stage="environment", stderr=stderr, exit_code=1))
    assert (result.category, result.rule_id) == ("contract", "CT002")
    assert any("depends on" in line for line in result.evidence)


def test_jq_false_in_run_is_implementation_with_diagnostic_output() -> None:
    result = classify(
        failure(
            stage="run",
            stdout="false\n",
            stderr="make[1]: *** [Makefile:75: sdk-plugin-run] Error 1\n",
            diagnostic={"ok": True, "output": {"return_code": 3, "stdout": "boom\n"}},
            plugin="script-bash",
        )
    )
    assert (result.category, result.rule_id) == ("implementation", "IM001")
    assert any('"return_code": 3' in line for line in result.evidence)


def test_run_success_without_output_is_cli_output_contract() -> None:
    result = classify(
        failure(stage="run", stdout="false\n", stderr=MAKE_ERROR, diagnostic={"ok": True})
    )
    assert (result.category, result.rule_id) == ("contract", "CT003")


def test_jq_false_with_failed_run_document_is_not_an_output_mismatch() -> None:
    result = classify(
        failure(stage="run", stdout="false\n", stderr=MAKE_ERROR, diagnostic={"ok": False})
    )
    assert result.rule_id != "IM001"


def test_jq_false_outside_run_is_cli_output_contract_only_with_a_document() -> None:
    build_json = {"artifactDigest": "not-a-digest", "workloadImage": "x"}
    result = classify(
        failure(stage="build-inspect", stdout="false\n", stderr=MAKE_ERROR, diagnostic=build_json)
    )
    assert (result.category, result.rule_id) == ("contract", "CT003")
    assert "captured --json keys: artifactDigest, workloadImage" in result.evidence


def test_jq_false_without_a_document_claims_nothing_deterministic() -> None:
    result = classify(failure(stage="validate", stdout="false\n", stderr=MAKE_ERROR))
    assert result.rule_id != "CT003"
    assert result.confidence != "deterministic"


def test_sdk_crash_outside_run_beats_a_network_flavoured_exception() -> None:
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "/s/.venv/lib/python3.12/site-packages/syntara_plugin/sdk/cli.py", '
        "line 10, in main\n"
        '  File "/s/.venv/lib/python3.12/site-packages/syntara_plugin/sdk/oci.py", '
        "line 20, in push\n"
        '  File "/s/.venv/lib/python3.12/site-packages/httpx/_transports/default.py", '
        "line 30, in handle_request\n"
        "httpx.ConnectError: [Errno 111] Connection refused\n"
        + MAKE_ERROR
    )
    result = classify(failure(stage="build-inspect", stderr=stderr, exit_code=2))
    assert (result.category, result.rule_id) == ("sdk-regression", "SR001")
    assert result.facts["network_signature"] == "ConnectError"
    assert "httpx.ConnectError: [Errno 111] Connection refused" in result.evidence
    assert "automatic retry" in result.cause


def test_network_signature_is_infra() -> None:
    document = {"error": {"kind": "RunExecutionError", "message": "httpx.ConnectError: boom"}}
    result = classify(failure(stage="run", diagnostic=document))
    assert (result.category, result.rule_id) == ("infra", "IN003")


def test_signatures_inside_the_echoed_input_are_ignored() -> None:
    document = {
        "ok": False,
        "input": {"url": "https://httpbin.org/get", "code": "raise ConnectError"},
        "error": {"code": "REMOTE_REJECTED", "message": "Script exited with code 2"},
    }
    result = classify(failure(stage="run", diagnostic=document))
    assert (result.category, result.rule_id) == ("implementation", "IM003")


def test_registry_stage_is_infra() -> None:
    result = classify(failure(stage="registry", note="registry did not answer"))
    assert (result.category, result.rule_id) == ("infra", "IN001")


def test_missing_tool_is_infra_not_fixture_drift() -> None:
    note = "required tool 'jq' was not found on PATH"
    result = classify(failure(stage="preflight", note=note))
    assert (result.category, result.rule_id) == ("infra", "IN006")
    assert "Install jq" in result.actions[0]


def test_registry_evidence_includes_the_engine_error() -> None:
    result = classify(
        failure(
            stage="registry",
            note="Start temporary OCI registry exited with code 126",
            stderr="Error: rootlessport listen tcp 0.0.0.0:5000: bind: address already in use\n",
        )
    )
    assert result.rule_id == "IN001"
    assert any("address already in use" in line for line in result.evidence)


def test_timeout_is_inferred_infra() -> None:
    result = classify(failure(stage="build-inspect", timed_out=True, exit_code=-9))
    assert (result.rule_id, result.confidence) == ("IN002", "inferred")


def test_preflight_is_fixture_drift() -> None:
    result = classify(failure(stage="preflight", note="missing http-request/example.json"))
    assert (result.category, result.rule_id) == ("fixture-drift", "FD001")


def test_unknown_plugin_in_fixture_scripts_is_fixture_drift() -> None:
    stderr = "Set PLUGIN to one of: http-request script-python script-bash\n"
    result = classify(failure(stage="validate", stderr=stderr, plugin="new-step"))
    assert (result.category, result.rule_id) == ("fixture-drift", "FD002")


def test_candidate_not_installed_is_fixture_drift() -> None:
    note = "candidate not installed: syntara-plugin-sdk 0.1.0a2 is installed from an index"
    result = classify(failure(stage="build-inspect", note=note))
    assert (result.category, result.rule_id) == ("fixture-drift", "FD003")


def test_inconsistent_wheels_is_sdk_regression() -> None:
    note = "candidate wheels are inconsistent: syntara-plugin-sdk 1 requires contracts==2"
    result = classify(failure(stage="environment", note=note))
    assert (result.category, result.rule_id) == ("sdk-regression", "SR002")


def test_runtime_abi_signature_is_contract() -> None:
    stderr = "RuntimeError: The plugin container does not support the requested runtime ABI.\n"
    result = classify(failure(stage="run", stderr=stderr))
    assert (result.category, result.rule_id) == ("contract", "CT001")


TRACEBACK_STEP_TYPES = """Traceback (most recent call last):
  File "/r/step-types/.venv/lib/python3.12/site-packages/syntara_plugin/runtime/server.py", line 10, in serve
    step.run()
  File "/r/step-types/http-request/src/syntara_node_http_request/step.py", line 42, in run
    raise KeyError("url")
KeyError: 'url'
"""

TRACEBACK_SDK = """Traceback (most recent call last):
  File "/r/step-types/http-request/src/syntara_node_http_request/step.py", line 42, in run
    context.emit()
  File "/r/step-types/.venv/lib/python3.12/site-packages/syntara_plugin/runtime/context.py", line 7, in emit
    raise AttributeError("gone")
AttributeError: gone
"""


def test_deepest_step_types_frame_is_inferred_implementation() -> None:
    result = classify(failure(stage="run", stderr=TRACEBACK_STEP_TYPES, exit_code=1))
    assert (result.category, result.rule_id, result.confidence) == (
        "implementation",
        "IM002",
        "inferred",
    )
    assert result.frames[0].origin == "step-types"


def test_deepest_sdk_frame_is_inferred_sdk_regression() -> None:
    result = classify(failure(stage="run", stderr=TRACEBACK_SDK, exit_code=1))
    assert (result.category, result.rule_id) == ("sdk-regression", "SR001")


def test_traceback_inside_json_strings_is_parsed() -> None:
    document = {"error": {"kind": "RunExecutionError", "traceback": TRACEBACK_SDK}}
    frames = parse_frames(failure(stage="run", stdout=json.dumps(document)))
    assert [frame.origin for frame in frames] == ["sdk", "step-types"]


def test_unknown_fallback_never_raises() -> None:
    result = classify(failure(stage="validate", stderr="something odd\n", exit_code=7))
    assert (result.category, result.rule_id, result.confidence) == (
        "unknown",
        "UN000",
        "unclassified",
    )
    assert result.evidence == ("something odd",)


def test_classification_is_deterministic() -> None:
    stderr = "error: build contains unsupported setting(s): a, b\n"
    first = classify(failure(stderr=stderr))
    second = classify(failure(stderr=stderr))
    assert first == second


@pytest.mark.parametrize(
    ("path", "origin"),
    [
        ("/x/site-packages/syntara_plugin/sdk/cli.py", "sdk"),
        ("/repo/sdks/python/packages/sdk/src/syntara_plugin/sdk/cli.py", "sdk"),
        ("/s/http-request/src/syntara_node_http_request/step.py", "step-types"),
        ("/x/site-packages/httpx/_client.py", "third-party"),
        ("/usr/lib/python3.12/json/decoder.py", "stdlib"),
        ("<frozen runpy>", "stdlib"),
        ("/elsewhere/file.py", "unknown"),
    ],
)
def test_frame_origin(path: str, origin: str) -> None:
    assert frame_origin(path) == origin


def test_frame_origin_uses_the_step_types_checkout(tmp_path: Path) -> None:
    assert frame_origin(str(tmp_path / "script-runtime" / "x.py"), tmp_path) == "step-types"
    assert frame_origin(str(tmp_path / ".venv" / "lib" / "x.py"), tmp_path) == "unknown"


def test_classifier_bug_falls_back_to_unclassified(monkeypatch: pytest.MonkeyPatch) -> None:
    import classifier

    def broken(failure: Failure, frames: object) -> None:
        raise RuntimeError("rule bug")

    monkeypatch.setattr(classifier, "RULES", (broken,))
    result = classify(failure(stderr="anything\n"))
    assert (result.rule_id, result.confidence) == ("UN000", "unclassified")
    assert "classifier itself failed" in result.what


def test_dependency_resolution_evidence_keeps_the_decisive_line() -> None:
    stderr = (
        "  x No solution found when resolving dependencies:\n"
        "  ╰─▶ Because there is no version of syntara-plugin-contracts==0.1.0a4 and\n"
        "      syntara-plugin-sdk==0.1.0a4 depends on syntara-plugin-contracts==0.1.0a4,\n"
    )
    result = classify(failure(stage="environment", stderr=stderr, exit_code=1))
    assert any("there is no version of" in line for line in result.evidence)


def test_outcome_word() -> None:
    from classifier import outcome_word

    assert outcome_word("infra") == "not verified"
    assert outcome_word("fixture-drift") == "not verified"
    assert outcome_word("misconfiguration") == "incompatible"


def test_registry_timeout_is_a_timeout_and_never_retried() -> None:
    result = classify(failure(stage="registry", timed_out=True, note="timed out after 5m00s"))
    assert result.rule_id == "IN002"


def test_text_compiler_diagnostic_from_build_is_classified() -> None:
    # Real output of build after only the removed flags are dropped (step-types PR 22).
    stderr = (
        "error: IMAGE_BINDING_MISSING: No explicit immutable binding was supplied for "
        "'http-request-runner'.\n" + MAKE_ERROR
    )
    result = classify(failure(stage="build-inspect", stderr=stderr, exit_code=2))
    assert (result.category, result.rule_id, result.confidence) == (
        "misconfiguration",
        "MC006",
        "deterministic",
    )
    assert result.facts == {"codes": ["IMAGE_BINDING_MISSING"]}
    assert any("RuntimeBinding" in action for action in result.actions)


def test_settings_error_text_is_not_mistaken_for_a_diagnostic_code() -> None:
    result = classify(failure(stderr="error: build contains unsupported setting(s): workload\n"))
    assert result.rule_id == "MC001"
