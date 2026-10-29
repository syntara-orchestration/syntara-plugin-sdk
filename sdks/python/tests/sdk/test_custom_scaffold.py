"""End-to-end checks for custom-runner workspace generation."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from syntara_plugin.runtime import SchemaDriftError
from syntara_plugin.sdk.cli import main
from syntara_plugin.testing import StepBindingsFactory


@pytest.mark.parametrize("fields", [None, "title,issue-count"])
def test_custom_workspace_runs_and_generated_tests_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fields: str | None,
) -> None:
    workspace = tmp_path / "workspace"
    name = "with-fields" if fields else "without-fields"
    args = [
        "init",
        str(workspace),
        "--runner",
        "custom-runner",
        "--namespace",
        "acme",
        "--name",
        name,
        "--action",
        "do-work",
        "--version",
        "1.2.3",
    ]
    if fields:
        args += ["--fields", fields]
    assert main(args) == 0
    assert main(["validate", str(workspace / "plugin.yaml")]) == 0
    plugin = yaml.safe_load((workspace / "plugin.yaml").read_text())
    target = yaml.safe_load((workspace / "steps/do-work/manifest.yaml").read_text())
    assert plugin["spec"]["workload"]["image"] == f"{name}-workload"
    assert target["spec"]["runtime"]["kind"] == "custom-runner"
    assert target["spec"]["output"]["value"]["required"] == ["resource"]
    entry_point = target["spec"]["runtime"]["entryPoint"]
    monkeypatch.syspath_prepend(str(workspace))
    binding = StepBindingsFactory(strict=True).bind_step(entry_point, workspace=workspace)
    runtime = importlib.import_module(f"syntara_step_{name.replace('-', '_')}.runtime")
    assert runtime.create_bindings().step_identities == (f"acme.{name}.do-work",)
    assert binding.descriptor.step_identity == f"acme.{name}.do-work"
    inputs = {"resource": "record"}
    if fields:
        inputs.update({"title": "example", "issue-count": "5"})
    assert (
        main(["run", "--manifest", str(workspace), "do-work", "--input", json.dumps(inputs)]) == 0
    )
    assert '"resource": "record"' in capsys.readouterr().out
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_custom_workspace_field_map_and_determinism(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fields = {
        "issue-count": {"type": "integer", "minimum": 1, "required": True},
        "details": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
            "additionalProperties": False,
        },
        "schema": {"type": "string", "default": "low", "enum": ["low", "high"]},
        "tags": {"type": "array", "items": {"type": "string"}},
    }
    workspaces = [tmp_path / "one", tmp_path / "two"]
    for workspace in workspaces:
        assert (
            main(
                [
                    "init",
                    str(workspace),
                    "--name",
                    "field-map",
                    "--runner",
                    "custom-runner",
                    "--fields",
                    json.dumps(fields),
                ]
            )
            == 0
        )
    snapshots = [
        {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        for root in workspaces
    ]
    assert snapshots[0] == snapshots[1]
    monkeypatch.syspath_prepend(str(workspaces[0]))
    binding = StepBindingsFactory(strict=True).bind_step(
        "syntara_step_field_map.step:ActionImplementation", workspace=workspaces[0]
    )
    model = binding.step.input_model().model_validate(
        {"resource": "r", "issue-count": 3, "details": {"label": "ok"}, "tags": ["a"]}
    )
    assert model.model_dump(by_alias=True)["issue-count"] == 3
    assert model.model_dump(by_alias=True)["schema"] == "low"


def test_generated_models_enforce_supported_constraints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "constraints"
    fields = {
        "severity": {"type": "string", "enum": ["low", "high"], "default": "low"},
        "count": {"type": "integer", "minimum": 1, "exclusiveMaximum": 5},
        "integer_limit": {"type": "integer", "maximum": 10.0},
        "ratio": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
        "large": {"type": "number", "maximum": 9007199254740992},
        "code": {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^(?=A)A+$"},
        "tags": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {"type": "string", "minLength": 2},
        },
    }
    assert (
        main(["init", str(workspace), "--runner", "custom-runner", "--fields", json.dumps(fields)])
        == 0
    )
    monkeypatch.syspath_prepend(str(workspace))
    model = importlib.import_module("syntara_step_constraints.step").ActionInput
    valid = {
        "resource": "r",
        "severity": "high",
        "count": 2,
        "ratio": 0.5,
        "code": "AA",
        "tags": ["ok"],
    }
    assert model.model_validate(valid).model_dump(by_alias=True)["severity"] == "high"
    assert model.model_validate({"resource": "r"}).severity == "low"
    for field, value in [
        ("resource", ""),
        ("severity", "critical"),
        ("count", 0),
        ("count", 5),
        ("count", "2"),
        ("ratio", 0),
        ("ratio", 2),
        ("code", "A"),
        ("code", "BBBB"),
        ("tags", []),
        ("tags", ["x"]),
        ("tags", ["ok"] * 3),
        ("severity", None),
        ("large", 9007199254740993),
        ("integer_limit", 11),
    ]:
        with pytest.raises(ValidationError):
            model.model_validate({**valid, field: value})
    StepBindingsFactory(strict=True).bind_step(
        "syntara_step_constraints.step:ActionImplementation", workspace=workspace
    )


@pytest.mark.parametrize(
    ("property_schema", "message"),
    [
        ({"type": "string", "format": "email"}, "format"),
        ({"type": "string", "anyOf": [{"type": "string"}]}, "anyOf"),
        ({"type": "object", "additionalProperties": {"type": "string"}}, "additionalProperties"),
        ({"type": "array", "items": {"type": "string", "format": "email"}}, "format"),
        ({"type": ["string", "null"]}, "type"),
        ({"type": "string", "default": 3}, "invalid default"),
        ({"type": "integer", "minimum": 2, "default": 1}, "invalid default"),
        ({"type": "integer", "default": 1.0}, "integer default"),
        ({"type": "integer", "exclusiveMaximum": 1.5}, "fractional integer bound"),
        ({"type": "integer", "maximum": 1e20}, "scientific notation"),
        ({"type": "number", "default": 1e-7}, "scientific notation"),
        ({"type": "string", "enum": ["low", "high"], "default": "other"}, "invalid default"),
        ({"type": "string", "enum": []}, "at least one value"),
        ({"type": "array", "items": {"type": "integer"}, "default": [1, "bad"]}, "invalid default"),
        (
            {"type": "object", "properties": {"nested": {"type": "string", "default": 1}}},
            "invalid default",
        ),
        ({"type": "number", "default": float("nan")}, "non-finite"),
        ({"type": "number", "default": float("inf")}, "non-finite"),
        ({"type": "array", "items": {"type": "number"}, "default": [float("-inf")]}, "non-finite"),
    ],
)
def test_custom_init_rejects_unsupported_schemas_and_invalid_defaults(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], property_schema: dict, message: str
) -> None:
    workspace = tmp_path / "invalid"
    fields = {"value": property_schema}
    assert (
        main(["init", str(workspace), "--runner", "custom-runner", "--fields", json.dumps(fields)])
        == 2
    )
    assert message in capsys.readouterr().err
    assert not workspace.exists()


def test_generated_workspace_build_requires_image_binding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "binding"
    assert main(["init", str(workspace), "--runner", "custom-runner"]) == 0
    capsys.readouterr()
    assert main(["validate", str(workspace / "plugin.yaml"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["deferredImageBindings"] == ["binding-workload"]
    archive = workspace / "plugin.oci.tar"
    assert main(["build", str(workspace / "plugin.yaml"), "--output", str(archive)]) == 1
    assert "IMAGE_BINDING_MISSING" in capsys.readouterr().err
    assert not archive.exists()


def test_custom_schema_file_and_drift_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["resource", "count"],
        "properties": {
            "resource": {"type": "string", "minLength": 1},
            "count": {"type": "integer"},
        },
    }
    (tmp_path / "fields.json").write_text(json.dumps(schema), encoding="utf-8")
    workspace = tmp_path / "schema-file"
    assert (
        main(["init", str(workspace), "--runner", "custom-runner", "--fields", "@fields.json"]) == 0
    )
    monkeypatch.syspath_prepend(str(workspace))
    entry_point = "syntara_step_schema_file.step:ActionImplementation"
    StepBindingsFactory(strict=True).bind_step(entry_point, workspace=workspace)
    schema["properties"]["count"]["type"] = "string"
    (workspace / "schemas/request.input.yaml").write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(SchemaDriftError):
        StepBindingsFactory(strict=True).bind_step(entry_point, workspace=workspace)


def test_custom_field_names_that_look_like_yaml_values(tmp_path: Path) -> None:
    workspace = tmp_path / "yaml-fields"
    assert main(["init", str(workspace), "--runner", "custom-runner", "--fields", "yes,on"]) == 0
    schema = json.loads((workspace / "schemas/request.input.yaml").read_text())
    assert set(schema["properties"]) == {"resource", "yes", "on"}
    assert main(["validate", str(workspace / "plugin.yaml")]) == 0


def test_custom_field_names_cannot_shadow_model_symbols(tmp_path: Path) -> None:
    workspace = tmp_path / "symbols"
    assert (
        main(
            [
                "init",
                str(workspace),
                "--runner",
                "custom-runner",
                "--fields",
                "Field,str,ActionInputField0",
            ]
        )
        == 0
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "syntara_plugin.sdk.cli",
            "run",
            "--manifest",
            str(workspace),
            "--input",
            '{"resource":"record","Field":"x","str":"y","ActionInputField0":"z"}',
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_plugin_name_does_not_shadow_the_sdk_namespace(tmp_path: Path) -> None:
    workspace = tmp_path / "plugin"
    assert main(["init", str(workspace), "--runner", "custom-runner"]) == 0
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "syntara_plugin.sdk.cli",
            "run",
            "--manifest",
            str(workspace),
            "--input",
            '{"resource":"record"}',
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_custom_init_rejects_invalid_input_and_existing_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "existing"
    workspace.mkdir()
    original = workspace / "keep.txt"
    original.write_text("keep", encoding="utf-8")
    assert main(["init", str(workspace), "--runner", "custom-runner"]) == 2
    assert original.read_text(encoding="utf-8") == "keep"
    invalid = tmp_path / "invalid"
    assert main(["init", str(invalid), "--runner", "custom-runner", "--fields", "resource"]) == 2
    assert not invalid.exists()


def test_custom_long_name_and_prerelease_build_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "long"
    name = "a" * 63
    assert (
        main(
            [
                "init",
                str(workspace),
                "--runner",
                "custom-runner",
                "--name",
                name,
                "--version",
                "1.2.3-preview.1",
            ]
        )
        == 0
    )
    assert main(["validate", str(workspace / "plugin.yaml")]) == 0
    settings = yaml.safe_load((workspace / "syntara-plugin.yaml").read_text())
    assert len(settings["build"]["workload"]["imageId"]) <= 63
    assert 'version = "1.2.3.dev0+preview.1"' in (workspace / "pyproject.toml").read_text()
