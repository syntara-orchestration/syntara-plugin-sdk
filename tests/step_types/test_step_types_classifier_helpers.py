"""Classifier tests for the infra, fixture-drift, and shared-helper branches.

``test_step_types_classifier.py`` pins the signature rules one by one and
``test_step_types_real_failures.py`` pins the captured run documents. This
module covers the branches those suites leave open: the archive-script arm of
FD002, tracebacks no rule can locate, the shared helpers behind them, and the
``Classification`` presentation the report prints.
"""

from __future__ import annotations

import json
from pathlib import Path

from classifier import Failure, _decode, classify, parse_frames, reproduce_hint

FIXTURES = Path(__file__).parent / "fixtures"


def failure(**fields: object) -> Failure:
    base = {"stage": "validate", "plugin": "http-request", "exit_code": 2}
    base.update(fields)
    return Failure(**base)  # type: ignore[arg-type]


def run_failure(plugin: str, diagnostic: dict) -> Failure:
    # The run target prints only jq's verdict; the analysis reads the re-run document.
    return Failure(
        stage="run",
        plugin=plugin,
        exit_code=2,
        stdout="false\n",
        stderr="make[1]: *** [Makefile:75: sdk-plugin-run] Error 1\n",
        diagnostic=diagnostic,
        step_types_dir=Path("/work/step-types"),
    )


def test_archive_script_rejecting_the_plugin_is_fixture_drift() -> None:
    # step-types' assert_plugin_archive.py is argparse-based: an unregistered
    # plugin dies there with "invalid choice" before any SDK code runs.
    invalid = (
        "assert_plugin_archive.py: error: argument plugin: invalid choice: 'new-step' "
        "(choose from 'http-request', 'script-python', 'script-bash')"
    )
    stderr = (
        f"usage: assert_plugin_archive.py [-h] plugin\n{invalid}\n"
        "make[1]: *** [Makefile:92: sdk-plugin-archives] Error 2\n"
    )
    result = classify(failure(stage="build-inspect", plugin="new-step", stderr=stderr))
    assert (result.category, result.rule_id, result.confidence) == (
        "fixture-drift",
        "FD002",
        "deterministic",
    )
    assert result.evidence == (invalid,)
    assert "'new-step'" in result.what


def test_crash_in_the_console_script_itself_blames_neither_side() -> None:
    # A broken entry point dies inside .venv/bin/syntara-plugin before any SDK
    # frame runs; the one frame the traceback has is on neither side.
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "/work/step-types/.venv/bin/syntara-plugin", line 6, in <module>\n'
        "    from syntara_plugin.sdk.cli import main\n"
        "ImportError: cannot import name 'main' from 'syntara_plugin.sdk.cli' "
        "(/work/step-types/.venv/lib/python3.12/site-packages/syntara_plugin/sdk/cli.py)\n"
    )
    result = classify(failure(stderr=stderr, step_types_dir=Path("/work/step-types")))
    assert (result.category, result.rule_id) == ("unknown", "UN000")
    assert [frame.origin for frame in result.frames] == ["unknown"]
    assert result.frames[0].path == "/work/step-types/.venv/bin/syntara-plugin"


def test_network_flavoured_crash_on_neither_side_is_infra_and_keeps_frames() -> None:
    # The captured tail of a build-inspect push failure: the frames that would
    # name a side were truncated away, so the network flavour decides, and the
    # frames stay on the report for manual reading.
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "/work/step-types/.venv/lib/python3.12/site-packages/httpx/_client.py", '
        "line 1743, in send\n"
        "    response = self._send_single_request(request)\n"
        '  File "/work/step-types/.venv/lib/python3.12/site-packages/httpx/_transports/'
        'default.py", line 30, in handle_request\n'
        "    raise ConnectError(str(exc)) from exc\n"
        "httpx.ConnectError: [Errno 111] Connection refused\n"
        "make[1]: *** [Makefile:85: sdk-plugin-build-inspect] Error 2\n"
    )
    result = classify(failure(stage="build-inspect", stderr=stderr))
    assert (result.category, result.rule_id) == ("infra", "IN003")
    assert result.facts["signature"] == "ConnectError"
    assert any("[Errno 111]" in line for line in result.evidence)
    assert [frame.origin for frame in result.frames] == ["third-party", "third-party"]
    assert result.frames[0].path.endswith("httpx/_transports/default.py")


def test_located_crash_without_a_recognised_exception_line_quotes_frame_and_code_only() -> None:
    # next() on an exhausted iterator ends the traceback with a bare
    # StopIteration, which is not an Error/Exception/Exit/Failure line; only the
    # located frame and the reported error.code are quoted.
    step_source = "/work/step-types/http-request/src/syntara_node_http_request/__init__.py"
    document = {
        "ok": False,
        "error": {"code": "OUTCOME_UNKNOWN", "message": "The step did not complete."},
        "errorDetail": (
            "Traceback (most recent call last):\n"
            '  File "/work/step-types/.venv/lib/python3.12/site-packages/syntara_plugin/'
            'runtime/execution.py", line 212, in execute\n'
            '  File "/work/step-types/.venv/lib/python3.12/site-packages/syntara_plugin/'
            'runtime/step.py", line 142, in execute\n'
            f'  File "{step_source}", line 43, in run\n'
            "    item = next(iter(self.pool))\n"
            "StopIteration\n"
        ),
    }
    result = classify(run_failure("http-request", document))
    assert (result.category, result.rule_id) == ("implementation", "IM002")
    assert result.evidence == (
        f"{step_source}:43 in run",
        "error.code OUTCOME_UNKNOWN: The step did not complete.",
    )


def test_chained_traceback_deduplicates_the_shared_frame() -> None:
    # "During handling of the above exception..." repeats the step's frame; the
    # analysis must list it once, deepest first.
    step_source = "/work/step-types/http-request/src/syntara_node_http_request/__init__.py"
    sdk_source = (
        "/work/step-types/.venv/lib/python3.12/site-packages/syntara_plugin/runtime/context.py"
    )
    step_frame = f'  File "{step_source}", line 43, in run\n    item = next(iter(self.pool))\n'
    document = {
        "ok": False,
        "error": {"code": "OUTCOME_UNKNOWN", "message": "The step did not complete."},
        "errorDetail": (
            "Traceback (most recent call last):\n"
            f"{step_frame}"
            "StopIteration\n"
            "\n"
            "During handling of the above exception, another exception occurred:\n"
            "\n"
            "Traceback (most recent call last):\n"
            f"{step_frame}"
            f'  File "{sdk_source}", line 9, in emit\n'
            '    raise RuntimeError("emit failed")\n'
            "RuntimeError: emit failed\n"
        ),
    }
    frames = parse_frames(run_failure("http-request", document))
    assert [(frame.path, frame.line) for frame in frames] == [
        (sdk_source, 9),
        (step_source, 43),
    ]
    result = classify(run_failure("http-request", document))
    assert result.rule_id == "SR001"  # the sdk frame is the deepest
    assert [frame.path for frame in result.frames] == [sdk_source, step_source]


def test_decode_rejects_brace_text_that_is_not_json() -> None:
    assert _decode('{"ok": false}') == {"ok": False}
    assert _decode("{'ok': False}") is None  # a Python repr, not JSON
    assert _decode('{"ok": false, "error": {"code": "OUTCOME_TR') is None  # truncated
    assert _decode("false") is None


def test_truncated_json_on_stdout_stays_searchable() -> None:
    # The diagnostic re-run was killed mid-write, so its document never decoded;
    # the partial text on stdout must still carry the signature it contains.
    stdout = (
        '{"ok": false, "error": {"code": "OUTCOME_UNKNOWN", '
        '"message": "httpx.ConnectError: [Errno 111] Connection refused"'
    )
    stderr = "make[1]: *** [Makefile:75: sdk-plugin-run] Error 1\n"
    result = classify(failure(stage="run", stdout=stdout, stderr=stderr))
    assert (result.category, result.rule_id) == ("infra", "IN003")
    assert result.facts["signature"] == "ConnectError"


def test_analysis_names_the_rule_that_matched() -> None:
    result = classify(failure(stage="preflight", note="required tool 'jq' was not found on PATH"))
    assert result.analysis == "deterministic, rule IN006 missing-tool"
    assert result.origin == (
        "the machine running the check (tools, registry, container engine, or network)"
    )


def test_unclassified_analysis_and_origin_say_so() -> None:
    stderr = "make[1]: *** [Makefile:71: sdk-plugin-validate] Error 3\n"
    result = classify(failure(stderr=stderr, exit_code=3))
    assert result.analysis == "unclassified"
    assert result.origin == "not determined"


def test_to_dict_serialises_the_analysis_for_the_report() -> None:
    document = json.loads((FIXTURES / "run-step-crash.json").read_text(encoding="utf-8"))
    result = classify(run_failure("http-request", document))
    assert result.rule_id == "IM002"
    as_dict = result.to_dict()
    assert set(as_dict) == {
        "category",
        "rule_id",
        "rule_name",
        "confidence",
        "origin",
        "what",
        "evidence",
        "cause",
        "actions",
        "facts",
        "frames",
    }
    assert as_dict["facts"] == {"code": "OUTCOME_UNKNOWN"}
    assert as_dict["frames"][0] == {
        "path": "/work/step-types/http-request/src/syntara_node_http_request/__init__.py",
        "line": 43,
        "function": "run",
        "origin": "step-types",
    }
    assert len(as_dict["frames"]) == 3
    assert json.loads(json.dumps(as_dict)) == as_dict


def test_reproduce_hint_names_the_plugin_and_backs_the_unclassified_action() -> None:
    assert reproduce_hint("http-request") == (
        "Reproduce locally: make test-step-types STEP_TYPES_PLUGIN=http-request"
    )
    result = classify(failure(plugin="script-bash", stderr="odd failure\n"))
    assert result.actions == (reproduce_hint("script-bash"),)
