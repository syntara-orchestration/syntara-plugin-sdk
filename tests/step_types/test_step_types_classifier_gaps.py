"""Gap tests for the classifier's misconfiguration rules (MC001-MC006).

The failure inputs are real SDK message shapes: every stderr string was
captured from this repository's own ``syntara-plugin`` CLI run against a
step-types plugin workspace, and the JSON documents mirror the CLI's
``--json`` error report. The make lines are the real targets' behaviour: a
validate settings failure prints no document, so jq exits 4; a validate
compiler document makes jq print ``false`` and exit 1.
"""

from __future__ import annotations

from pathlib import Path

from classifier import Failure, classify, locate_settings

MAKE_VALIDATE_EMPTY = "make[1]: *** [Makefile:71: sdk-plugin-validate] Error 4\n"
MAKE_VALIDATE_FALSE = "make[1]: *** [Makefile:71: sdk-plugin-validate] Error 1\n"
MAKE_RUN_FALSE = "make[1]: *** [Makefile:75: sdk-plugin-run] Error 1\n"
MAKE_BUILD_ERROR = "make[1]: *** [Makefile:85: sdk-plugin-build-inspect] Error 1\n"


def failure(**fields: object) -> Failure:
    base = {"stage": "validate", "plugin": "http-request", "exit_code": 2}
    base.update(fields)
    return Failure(**base)  # type: ignore[arg-type]


def test_required_setting_is_deterministic_misconfiguration() -> None:
    # Captured from `syntara-plugin build --runtime-bindings ...` (recipe exit 1)
    # when the workload settings lack a value the candidate SDK now requires.
    result = classify(
        failure(
            stage="build-inspect",
            stderr="error: build.workloads.python-runtime requires: repository\n"
            + MAKE_BUILD_ERROR,
        )
    )
    assert (result.category, result.rule_id, result.confidence) == (
        "misconfiguration",
        "MC003",
        "deterministic",
    )
    assert (
        result.what
        == "The candidate SDK now requires repository under 'build.workloads.python-runtime'."
    )
    assert result.evidence == ("build.workloads.python-runtime requires: repository",)
    assert "mandatory" in result.cause


def test_validate_settings_semantics_error_is_rejected_workspace() -> None:
    # Captured from `syntara-plugin validate` (exit 2): the settings value
    # passed the schema but failed the candidate SDK's semantic check. The
    # CLI prints no document, so the make target fails with jq's exit 4.
    result = classify(
        failure(
            stderr="error: registry.allowInsecureLoopbackHttp must be a boolean\n"
            + MAKE_VALIDATE_EMPTY
        )
    )
    assert (result.category, result.rule_id, result.confidence) == (
        "misconfiguration",
        "MC004",
        "inferred",
    )
    assert result.evidence == ("error: registry.allowInsecureLoopbackHttp must be a boolean",)
    assert "during validation" in result.what


def test_compiler_diagnostic_evidence_appends_the_document_path() -> None:
    # Captured from `syntara-plugin validate --json` on the pinned step-types
    # http-request plugin after breaking spec.runtime.entryPoint: locations
    # deeper than the document root carry a JSON-pointer-style path.
    message = (
        r"spec/runtime/entryPoint: '1nv4lid:Step!!' does not match "
        r"'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_]"
        r"[A-Za-z0-9_]*$(?![\s\S])'"
    )
    document = {
        "diagnostics": [
            {
                "code": "SCHEMA_INVALID",
                "location": {
                    "column": 17,
                    "file": "manifest.yaml",
                    "line": 10,
                    "path": "$/spec/runtime/entryPoint",
                },
                "message": message,
                "remediation": "Correct the source document to satisfy the published contract.",
            }
        ],
        "ok": False,
    }
    result = classify(failure(stdout="false\n", stderr=MAKE_VALIDATE_FALSE, diagnostic=document))
    assert (result.category, result.rule_id, result.confidence) == (
        "misconfiguration",
        "MC006",
        "deterministic",
    )
    assert result.evidence == (
        f"SCHEMA_INVALID manifest.yaml:10 $/spec/runtime/entryPoint: {message}",
    )
    assert result.facts == {"codes": ["SCHEMA_INVALID"]}


def test_locate_settings_skips_missing_files_and_keeps_looking(tmp_path: Path) -> None:
    plugin = tmp_path / "http-request"
    plugin.mkdir()
    (plugin / "syntara-plugin.yaml").write_text(
        "apiVersion: syntara.io/v1alpha1\n"
        "kind: PluginBuildSettings\n"
        "manifest: plugin.yaml\n"
        "build:\n"
        "  workload:\n"
        "    engine: podman\n"
    )
    # plugin.yaml is absent (renamed manifest); the search must continue to
    # the settings file that is still there.
    locations = locate_settings(plugin, ["workload"], files=("plugin.yaml", "syntara-plugin.yaml"))
    assert locations == ["http-request/syntara-plugin.yaml:5 (workload)"]


def test_unsupported_setting_without_a_settings_file_reports_no_locations(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "http-request"
    plugin.mkdir()  # The settings file was renamed; there is nothing to point at.
    result = classify(
        failure(
            stderr="error: build contains unsupported setting(s): workload\n" + MAKE_VALIDATE_EMPTY,
            plugin_dir=plugin,
        )
    )
    assert (result.category, result.rule_id) == ("misconfiguration", "MC001")
    assert result.facts["keys"] == ["workload"]
    assert result.facts["locations"] == []
    assert result.facts["transform"] == "workload-to-workloads"
    assert result.what == "The candidate SDK rejects the setting(s) workload under 'build'."


def test_unsupported_setting_with_several_keys_locates_each(tmp_path: Path) -> None:
    plugin = tmp_path / "http-request"
    plugin.mkdir()
    (plugin / "syntara-plugin.yaml").write_text(
        "apiVersion: syntara.io/v1alpha1\n"
        "kind: PluginBuildSettings\n"
        "manifest: plugin.yaml\n"
        "staging: {}\n"
        "workload: {}\n"
    )
    # Captured from `syntara-plugin validate` (exit 2) with two unknown root keys.
    result = classify(
        failure(
            stderr="error: settings document contains unsupported setting(s): staging, workload\n"
            + MAKE_VALIDATE_EMPTY,
            plugin_dir=plugin,
        )
    )
    assert (result.category, result.rule_id) == ("misconfiguration", "MC001")
    assert result.facts["keys"] == ["staging", "workload"]
    assert result.facts["locations"] == [
        "http-request/syntara-plugin.yaml:4 (staging)",
        "http-request/syntara-plugin.yaml:5 (workload)",
    ]
    assert "transform" not in result.facts
    assert "http-request/syntara-plugin.yaml:5 (workload)" in result.what


def test_unsupported_setting_under_a_non_build_label_has_no_transform(tmp_path: Path) -> None:
    plugin = tmp_path / "http-request"
    plugin.mkdir()
    (plugin / "syntara-plugin.yaml").write_text(
        "apiVersion: syntara.io/v1alpha1\n"
        "kind: PluginBuildSettings\n"
        "manifest: plugin.yaml\n"
        "registry:\n"
        "  origin: http://localhost:5000\n"
        "  mirrors: []\n"
    )
    # Captured from `syntara-plugin validate` (exit 2) with a stale registry key.
    result = classify(
        failure(
            stderr="error: registry contains unsupported setting(s): mirrors\n"
            + MAKE_VALIDATE_EMPTY,
            plugin_dir=plugin,
        )
    )
    assert result.facts == {
        "label": "registry",
        "keys": ["mirrors"],
        "locations": ["http-request/syntara-plugin.yaml:6 (mirrors)"],
    }


def test_schema_violation_inside_run_json_beats_the_settings_error_kind() -> None:
    # Captured from `syntara-plugin run --json` (exit 2): the settings loader
    # reports a schema failure as a SettingsError document, and the specific
    # MC002 signature must win over the generic run-settings rule.
    document = {
        "error": {
            "detail": [],
            "kind": "SettingsError",
            "message": "settings document schema validation failed: "
            "build/workloads/http-request: 'platform' is a required property",
            "nextSteps": [],
        },
        "ok": False,
    }
    result = classify(
        failure(stage="run", stdout="false\n", stderr=MAKE_RUN_FALSE, diagnostic=document)
    )
    assert (result.category, result.rule_id) == ("misconfiguration", "MC002")
    assert "build/workloads/http-request: 'platform' is a required property" in result.what


def test_compilation_error_kind_is_a_run_settings_error() -> None:
    # The run-settings rule also accepts the CompilationError kind. The current
    # CLI reports compile failures as a diagnostics document (MC006) instead,
    # so this pins the rule's own kind contract.
    document = {
        "error": {
            "kind": "CompilationError",
            "message": "TARGET_PATH_INVALID: 'missing-target.yaml' is not a regular file.",
        },
        "ok": False,
    }
    result = classify(
        failure(stage="run", stdout="false\n", stderr=MAKE_RUN_FALSE, diagnostic=document)
    )
    assert (result.category, result.rule_id, result.confidence) == (
        "misconfiguration",
        "MC005",
        "deterministic",
    )
    assert result.what == (
        "syntara-plugin run reported CompilationError: "
        "TARGET_PATH_INVALID: 'missing-target.yaml' is not a regular file."
    )


def test_multi_word_labels_are_not_truncated() -> None:
    # The real root label is "settings document"; the label class must span
    # the space instead of reporting the truncated tail.
    result = classify(
        failure(stderr="error: settings document contains unsupported setting(s): workload\n"
                + MAKE_VALIDATE_EMPTY)
    )
    assert result.facts["label"] == "settings document"
    assert "under 'settings document'" in result.what
