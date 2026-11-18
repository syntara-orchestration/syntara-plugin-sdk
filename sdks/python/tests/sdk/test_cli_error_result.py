"""CLI failures expose the shared error vocabulary in JSON output."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from syntara_plugin.runtime import error_result_base_schema
from syntara_plugin.sdk.cli import main


@pytest.mark.parametrize(("command", "expected"), [("validate", "VALIDATION_FAILED"), ("build", "VALIDATION_FAILED"), ("run", "INVALID_INPUT")])
def test_missing_manifest_reports_shared_error(tmp_path: Path, capsys, command: str, expected: str) -> None:
    missing = tmp_path / "missing.yaml"
    argv = [command, "--json"]
    argv.extend(["--manifest", str(missing)] if command == "run" else [str(missing)])
    if command == "build":
        argv.extend(["--output", str(tmp_path / "output.tar")])
    assert main(argv) != 0
    payload = json.loads(capsys.readouterr().out)
    assert not payload["ok"]
    assert Draft202012Validator(error_result_base_schema()).is_valid(payload["errorResult"])
    assert payload["errorResult"]["code"] == expected


def test_build_option_error_is_json(tmp_path: Path, capsys) -> None:
    assert main(["build", "--json", "--binding-output", str(tmp_path / "x")]) != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["errorResult"]["code"] == "MISCONFIGURATION"


def test_argument_parser_error_is_json(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["build", "--json", "--unknown-option"])
    assert error.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["errorResult"]["code"] == "INVALID_INPUT"


def test_build_bad_output_parent_is_json(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    assert main(["init", str(workspace), "--namespace", "acme"]) == 0
    capsys.readouterr()
    parent = tmp_path / "file"
    parent.write_text("not a directory")
    assert main(["build", str(workspace / "plugin.yaml"), "--output", str(parent / "out.tar"), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["errorResult"]["code"] == "MISCONFIGURATION"
