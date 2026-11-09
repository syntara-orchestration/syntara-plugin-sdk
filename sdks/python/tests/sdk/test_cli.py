"""End-to-end checks for the thin offline authoring CLI."""

from __future__ import annotations

import base64
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from syntara_plugin.sdk import RegistryCredentials
from syntara_plugin.sdk.cli import (
    _build_runtime_binding_images,
    _catalog_entry,
    _credentials_from_auth_file,
    _publish_release,
    _read_release_inventory,
    _release_inventory_path,
    _runtime_binding_index_verifier,
    main,
)
from syntara_plugin.sdk.settings import SETTINGS_FILENAME, SettingsError

_WORKLOAD_DIGEST = "sha256:" + "a" * 64


def test_registry_auth_file_loads_only_the_selected_registry_credential(tmp_path: Path) -> None:
    """A local Docker/Podman auth file supplies ephemeral publication credentials."""
    auth_file = tmp_path / "auth.json"
    token = base64.b64encode(b"publisher:local-password").decode("ascii")
    auth_file.write_text(
        json.dumps({"auths": {"registry.example.test": {"auth": token}}}),
        encoding="utf-8",
    )

    credentials = _credentials_from_auth_file(auth_file, "https://registry.example.test")

    assert credentials.username == "publisher"
    assert credentials.password == "local-password"


def test_registry_auth_file_supports_configured_loopback_http_origin(tmp_path: Path) -> None:
    """The configured HTTP origin is itself a valid Podman auth-file key."""
    auth_file = tmp_path / "auth.json"
    token = base64.b64encode(b"publisher:local-password").decode("ascii")
    auth_file.write_text(
        json.dumps({"auths": {"http://localhost:8443": {"auth": token}}}),
        encoding="utf-8",
    )

    credentials = _credentials_from_auth_file(auth_file, "http://localhost:8443")

    assert credentials.username == "publisher"
    assert credentials.password == "local-password"


def test_runtime_binding_index_predecessor_requires_configured_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "sha256:" + "a" * 64
    arguments = SimpleNamespace(
        cosign_verify_key=None,
        expected_binding_index_digest=None,
        allow_unverified_binding_index=False,
        registry_origin="https://registry.example.test",
    )

    untrusted_verifier = _runtime_binding_index_verifier(
        arguments, "acme/plugins/issue-tracker"
    )
    with pytest.raises(SettingsError, match="requires --cosign-verify-key"):
        untrusted_verifier(digest)

    arguments.allow_unverified_binding_index = True
    _runtime_binding_index_verifier(arguments, "acme/plugins/issue-tracker")(digest)

    arguments.allow_unverified_binding_index = False
    arguments.expected_binding_index_digest = digest
    pinned_verifier = _runtime_binding_index_verifier(
        arguments, "acme/plugins/issue-tracker"
    )
    pinned_verifier(digest)
    mismatched_verifier = _runtime_binding_index_verifier(
        arguments, "acme/plugins/issue-tracker"
    )
    with pytest.raises(SettingsError, match="does not match the expected digest"):
        mismatched_verifier("sha256:" + "b" * 64)

    arguments.expected_binding_index_digest = None
    arguments.cosign_verify_key = Path("cosign.pub")
    verified: list[tuple[str, Path]] = []

    def record_verify(reference: str, *, key: Path) -> None:
        verified.append((reference, key))

    monkeypatch.setattr("syntara_plugin.sdk.cli.verify_oci_reference", record_verify)
    _runtime_binding_index_verifier(arguments, "acme/plugins/issue-tracker")(digest)
    assert verified == [
        (f"registry.example.test/acme/plugins/issue-tracker@{digest}", Path("cosign.pub"))
    ]


def test_init_creates_a_compilable_workspace_and_validate_reports_its_digest(
    tmp_path: Path,
    capsys,
) -> None:
    """Generated YAML goes through the same compiler as every other authoring path."""
    workspace = tmp_path / "status-tools"

    assert main(["init", str(workspace), "--namespace", "acme"]) == 0
    assert (workspace / "plugin.yaml").is_file()
    assert (workspace / SETTINGS_FILENAME).is_file()
    assert (workspace / "steps/request/manifest.yaml").is_file()
    assert (workspace / "schemas/request.input.yaml").is_file()
    generated_manifest = yaml.safe_load(
        (workspace / "steps/request/manifest.yaml").read_text(encoding="utf-8")
    )
    assert "error" in generated_manifest["spec"]

    assert main(["validate", str(workspace / "plugin.yaml"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert payload["ok"] is True
    assert payload["digest"].startswith("sha256:")
    assert payload["assetCount"] == 2


def test_init_accepts_field_names_and_generates_a_valid_schema(
    tmp_path: Path,
    capsys,
) -> None:
    """CLI scaffolding turns optional-field options into schema declarations."""
    workspace = tmp_path / "issue-tools"

    assert (
        main(
            [
                "init",
                str(workspace),
                "--fields",
                "title,issue_count",
            ]
        )
        == 0
    )
    capsys.readouterr()
    schema = yaml.safe_load(
        (workspace / "schemas/request.input.yaml").read_text(encoding="utf-8")
    )

    assert schema["required"] == ["resource"]
    assert set(schema["properties"]) == {"resource", "title", "issue_count"}
    assert schema["properties"]["title"]["description"] == "Optional input field."

    assert main(["validate", str(workspace / "plugin.yaml"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["ok"] is True


def test_init_accepts_an_inline_json_schema_with_required_fields(
    tmp_path: Path,
    capsys,
) -> None:
    """A JSON parameter can define the complete generated input schema."""
    workspace = tmp_path / "issue-tools"
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["resource", "title"],
        "properties": {
            "resource": {"type": "string", "minLength": 1},
            "title": {"type": "string", "minLength": 1},
            "priority": {
                "type": "string",
                "default": "low",
                "enum": ["low", "medium", "high"],
            },
        },
    }

    assert main(["init", str(workspace), "--fields", json.dumps(schema)]) == 0
    capsys.readouterr()
    generated = yaml.safe_load(
        (workspace / "schemas/request.input.yaml").read_text(encoding="utf-8")
    )

    assert generated == schema
    assert main(["validate", str(workspace / "plugin.yaml"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["ok"] is True


def test_init_accepts_a_json_field_map_with_standard_schema_keys(
    tmp_path: Path,
    capsys,
) -> None:
    """A field map can describe each property without a full JSON Schema."""
    workspace = tmp_path / "issue-tools"
    field_map = {
        "url": {
            "description": "description",
            "type": "string",
            "minLength": 1,
            "maxLength": 50,
        }
    }

    assert main(["init", str(workspace), "--fields", json.dumps(field_map)]) == 0
    capsys.readouterr()
    schema = yaml.safe_load(
        (workspace / "schemas/request.input.yaml").read_text(encoding="utf-8")
    )

    assert schema["required"] == ["resource"]
    assert schema["properties"]["url"] == {
        "description": "description",
        "type": "string",
        "minLength": 1,
        "maxLength": 50,
    }
    assert main(["validate", str(workspace / "plugin.yaml"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["ok"] is True


def test_init_accepts_a_required_field_map_entry(tmp_path: Path, capsys) -> None:
    """Field-map entries can be marked required without a full schema."""
    workspace = tmp_path / "issue-tools"
    field_map = {
        "title": {
            "type": "string",
            "minLength": 1,
            "required": True,
        }
    }

    assert main(["init", str(workspace), "--fields", json.dumps(field_map)]) == 0
    capsys.readouterr()
    schema = yaml.safe_load(
        (workspace / "schemas/request.input.yaml").read_text(encoding="utf-8")
    )

    assert schema["required"] == ["resource", "title"]
    assert schema["properties"]["title"] == {
        "type": "string",
        "minLength": 1,
    }
    assert main(["validate", str(workspace / "plugin.yaml"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["ok"] is True


def test_init_accepts_a_json_schema_file(tmp_path: Path, capsys, monkeypatch) -> None:
    """An absolute schema path is accepted when it resolves inside the working directory."""
    workspace = tmp_path / "issue-tools"
    schema_path = tmp_path / "fields.json"
    schema = {
        "type": "object",
        "required": ["resource", "issue_count"],
        "properties": {
            "resource": {"type": "string", "minLength": 1},
            "issue_count": {"type": "integer", "minimum": 0},
        },
    }
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert main(["init", str(workspace), "--fields", str(schema_path)]) == 0
    capsys.readouterr()
    generated = yaml.safe_load(
        (workspace / "schemas/request.input.yaml").read_text(encoding="utf-8")
    )

    assert generated == schema
    assert main(["validate", str(workspace / "plugin.yaml"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["ok"] is True


def test_init_accepts_an_at_prefixed_json_schema_file(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """``@`` selects a JSON file. ``..`` inside one segment is not parent traversal."""
    workspace = tmp_path / "issue-tools"
    schema_path = tmp_path / "fields..json"
    schema = {
        "type": "object",
        "required": ["resource", "issue_count"],
        "properties": {
            "resource": {"type": "string", "minLength": 1},
            "issue_count": {"type": "integer", "minimum": 0},
        },
    }
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert main(["init", str(workspace), "--fields", "@fields..json"]) == 0
    capsys.readouterr()
    generated = yaml.safe_load(
        (workspace / "schemas/request.input.yaml").read_text(encoding="utf-8")
    )

    assert generated == schema


def test_init_rejects_fields_file_path_traversal(tmp_path: Path, capsys, monkeypatch) -> None:
    """LLM-supplied ``--fields`` paths cannot escape via ``..`` or a symlink."""
    sentinel = "fields-schema-sentinel"
    secret = tmp_path / "secret"
    secret.mkdir()
    schema = {
        "type": "object",
        "required": ["resource"],
        "properties": {"resource": {"type": "string", "description": sentinel}},
    }
    (secret / "fields.json").write_text(json.dumps(schema), encoding="utf-8")
    work = tmp_path / "work"
    (work / "nested").mkdir(parents=True)
    (work / "local.json").write_text(json.dumps(schema), encoding="utf-8")
    (work / "link.json").symlink_to(Path("..") / "secret" / "fields.json")
    (work / "linkdir").symlink_to(secret, target_is_directory=True)
    (work / "nested" / "up.json").symlink_to(Path("..") / ".." / "secret" / "fields.json")
    monkeypatch.chdir(work)
    absolute = work / ".." / "secret" / "fields.json"
    outside = (secret / "fields.json").resolve()
    arguments = [
        "../secret/fields.json",
        "@../secret/fields.json",
        "./../secret/fields.json",
        "@./../secret/fields.json",
        "nested/../../secret/fields.json",
        "@nested/../../secret/fields.json",
        "nested/../local.json",
        "@nested/../local.json",
        str(absolute),
        f"@{absolute}",
        str(outside),
        f"@{outside}",
        "link.json",
        "@link.json",
        "linkdir/fields.json",
        "@linkdir/fields.json",
        "linkdir/missing.json",
        "@linkdir/missing.json",
        "nested/up.json",
        "@nested/up.json",
    ]

    for index, fields in enumerate(arguments):
        workspace = work / f"plugin-{index}"
        assert main(["init", str(workspace), "--fields", fields]) == 2, fields
        captured = capsys.readouterr()
        assert "path traversal" in captured.err
        assert sentinel not in captured.out
        assert sentinel not in captured.err
        assert not workspace.exists()


def test_init_accepts_a_fields_symlink_inside_the_working_directory(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A relative symlink is readable when its target stays in the working directory."""
    workspace = tmp_path / "issue-tools"
    schema_path = tmp_path / "fields.json"
    schema = {
        "type": "object",
        "required": ["resource", "issue_count"],
        "properties": {
            "resource": {"type": "string", "minLength": 1},
            "issue_count": {"type": "integer", "minimum": 0},
        },
    }
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    (tmp_path / "link.json").symlink_to(schema_path.name)
    monkeypatch.chdir(tmp_path)

    assert main(["init", str(workspace), "--fields", "link.json"]) == 0
    capsys.readouterr()
    generated = yaml.safe_load(
        (workspace / "schemas/request.input.yaml").read_text(encoding="utf-8")
    )

    assert generated == schema


def test_init_reads_a_fields_file_from_a_subdirectory(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Nested relative and absolute paths inside the working directory are readable."""
    schema = {
        "type": "object",
        "required": ["resource", "issue_count"],
        "properties": {
            "resource": {"type": "string", "minLength": 1},
            "issue_count": {"type": "integer", "minimum": 0},
        },
    }
    nested = tmp_path / "schemas" / "fields.json"
    nested.parent.mkdir()
    nested.write_text(json.dumps(schema), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    for index, fields in enumerate(["schemas/fields.json", f"@{nested}"]):
        workspace = tmp_path / f"issue-tools-{index}"
        assert main(["init", str(workspace), "--fields", fields]) == 0, fields
        capsys.readouterr()
        generated = yaml.safe_load(
            (workspace / "schemas/request.input.yaml").read_text(encoding="utf-8")
        )
        assert generated == schema


def test_init_rejects_a_missing_at_prefixed_fields_file(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """``@`` always names a file, so a missing one is an error rather than inline data."""
    workspace = tmp_path / "issue-tools"
    monkeypatch.chdir(tmp_path)

    assert main(["init", str(workspace), "--fields", "@missing.json"]) == 2
    assert "fields file does not exist: missing.json" in capsys.readouterr().err
    assert not workspace.exists()


def test_init_rejects_a_json_schema_that_does_not_require_resource(
    tmp_path: Path, capsys
) -> None:
    """The scaffold route still requires the built-in resource input."""
    workspace = tmp_path / "invalid-schema"
    schema = {
        "type": "object",
        "required": ["title"],
        "properties": {"title": {"type": "string"}},
    }

    assert main(["init", str(workspace), "--fields", json.dumps(schema)]) == 2
    assert "must require resource" in capsys.readouterr().err
    assert not workspace.exists()


def test_init_rejects_a_required_property_that_is_not_defined(
    tmp_path: Path, capsys
) -> None:
    """Every required input must have a matching property definition."""
    workspace = tmp_path / "invalid-schema"
    schema = {
        "type": "object",
        "required": ["resource", "title"],
        "properties": {"resource": {"type": "string", "minLength": 1}},
    }

    assert main(["init", str(workspace), "--fields", json.dumps(schema)]) == 2
    assert "must define every required property: title" in capsys.readouterr().err
    assert not workspace.exists()


@pytest.mark.parametrize(
    ("resource", "message"),
    [
        ({"type": "integer"}, "must define resource as a string"),
        ({"type": ["string", "integer"]}, "must define resource as a string"),
        (True, "must define resource as a string"),
        ({"type": "string"}, "must set resource minLength to at least 1"),
        ({"type": "string", "minLength": 0}, "must set resource minLength to at least 1"),
    ],
)
def test_init_rejects_a_json_schema_that_redefines_resource_unusably(
    tmp_path: Path, capsys, resource: object, message: str
) -> None:
    """The generated action renders resource into its path, so it must stay usable."""
    workspace = tmp_path / "invalid-schema"
    schema = {
        "type": "object",
        "required": ["resource"],
        "properties": {"resource": resource},
    }

    assert main(["init", str(workspace), "--fields", json.dumps(schema)]) == 2
    assert message in capsys.readouterr().err
    assert not workspace.exists()


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ("title,title", "fields must be unique"),
        ("resource", "field 'resource' is already included"),
        ("1-title", "field is not valid"),
        ("[1]", "fields JSON array entries must be strings"),
        ("[{}]", "fields JSON array entries must be strings"),
        ("[1, 1]", "fields JSON array entries must be strings"),
    ],
)
def test_init_rejects_invalid_field_names(
    tmp_path: Path, capsys, fields: str, message: str
) -> None:
    """Invalid or duplicate fields are rejected before files are written."""
    workspace = tmp_path / "invalid-fields"
    arguments = ["init", str(workspace), "--fields", fields]

    assert main(arguments) == 2
    assert message in capsys.readouterr().err
    assert not workspace.exists()


def test_manifest_adjacent_settings_win_over_an_unrelated_cwd_settings_file(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A plugin root, not the caller's directory, owns its selected settings."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    sibling_output = workspace / "dist/from-plugin-root.oci.tar"
    cwd_output = tmp_path / "dist/from-cwd.oci.tar"
    (workspace / SETTINGS_FILENAME).write_text(
        "\n".join(
            (
                "apiVersion: syntara.io/v1alpha1",
                "kind: PluginBuildSettings",
                "manifest: plugin.yaml",
                "build:",
                f"  artifactOutput: {sibling_output}",
                "",
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / SETTINGS_FILENAME).write_text(
        "\n".join(
            (
                "apiVersion: syntara.io/v1alpha1",
                "kind: PluginBuildSettings",
                f"manifest: {workspace / 'plugin.yaml'}",
                "build:",
                f"  artifactOutput: {cwd_output}",
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert main(["build", str(workspace / "plugin.yaml"), "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["archive"] == str(sibling_output)
    assert sibling_output.is_file()
    assert not cwd_output.exists()


def test_inspect_prints_the_canonical_descriptor_for_the_generated_workspace(
    tmp_path: Path,
    capsys,
) -> None:
    """Inspection is deterministic canonical output, not a second parser."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    assert main(["inspect", str(workspace / "plugin.yaml")]) == 0
    descriptor = json.loads(capsys.readouterr().out)
    assert descriptor["metadata"]["name"] == "status-tools"
    assert descriptor["spec"]["targets"][0]["runtime"]["implementation"] == "syntara.http.request@1.0.0"


def test_inspect_reads_the_verified_metadata_from_a_built_archive(tmp_path: Path, capsys) -> None:
    """Archive inspection does not need the source workspace to still exist."""
    workspace = tmp_path / "status-tools"
    archive = tmp_path / "status-tools.oci.tar"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    assert main(["build", str(workspace / "plugin.yaml"), "--output", str(archive)]) == 0
    capsys.readouterr()

    assert main(["inspect", str(archive)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["artifactDigest"].startswith("sha256:")
    assert payload["compiledDigest"].startswith("sha256:")


def test_inspect_rejects_source_only_image_bindings_for_an_archive(tmp_path: Path, capsys) -> None:
    """Archive inspection cannot silently ignore an option that affects source compilation."""
    workspace = tmp_path / "status-tools"
    archive = tmp_path / "status-tools.oci.tar"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    assert main(["build", str(workspace / "plugin.yaml"), "--output", str(archive)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "inspect",
                str(archive),
                "--image-binding",
                "workload=quay.io/acme/test@sha256:" + "a" * 64,
            ]
        )
        == 2
    )
    assert "applies only" in capsys.readouterr().err


def test_init_refuses_to_overwrite_an_existing_path(tmp_path: Path, capsys) -> None:
    """A typo cannot replace an author's workspace."""
    workspace = tmp_path / "existing"
    workspace.mkdir()
    marker = workspace / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    assert main(["init", str(workspace)]) == 2
    assert marker.read_text(encoding="utf-8") == "keep"
    assert "refusing to overwrite" in capsys.readouterr().err


def test_init_rejects_directory_traversal(tmp_path: Path, capsys) -> None:
    """Workspace creation never interprets an author-supplied parent traversal."""
    workspace = tmp_path / "safe-parent" / ".." / "escaped"

    assert main(["init", str(workspace)]) == 2
    assert not (tmp_path / "escaped").exists()
    assert "path traversal" in capsys.readouterr().err


def test_validate_reports_compiler_diagnostics_without_a_traceback(tmp_path: Path, capsys) -> None:
    """Invalid source produces the compiler's stable structured diagnostic surface."""
    missing_root = tmp_path / "missing.yaml"

    assert main(["validate", str(missing_root), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["diagnostics"][0]["code"] == "DOCUMENT_UNREADABLE"


@pytest.mark.parametrize(
    "entry_point", ["steps..bad:Run", "steps.bad.:Run", "steps:Run.More"]
)
def test_validate_rejects_malformed_custom_runner_entry_points(
    tmp_path: Path, capsys, entry_point: str
) -> None:
    """The CLI rejects malformed entry points before an artifact can be built."""
    workspace = tmp_path / "malformed-entry-point"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()

    manifest_path = workspace / "steps/request/manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["spec"]["runtime"]["entryPoint"] = entry_point
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    assert main(["validate", str(workspace / "plugin.yaml"), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any(diagnostic["code"] == "SCHEMA_INVALID" for diagnostic in payload["diagnostics"])
    assert "entryPoint" in json.dumps(payload)
    assert not (workspace / "dist").exists()


def test_validate_defers_a_custom_runner_image_binding_until_build(
    tmp_path: Path, capsys
) -> None:
    """Source validation does not need an image that has not been built yet."""
    workspace = tmp_path / "custom-runner"
    workspace.mkdir()
    (workspace / "plugin.yaml").write_text(
        """\
apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: acme
  name: custom-runner
  version: 0.1.0
  displayName: Custom runner
  description: A custom runner plugin.
spec:
  documentation:
    path: docs/README.md
  workload:
    image: custom-runner-image
  targets:
    - steps/request/manifest.yaml
""",
        encoding="utf-8",
    )
    action = workspace / "steps/request/manifest.yaml"
    action.parent.mkdir(parents=True)
    action.write_text(
        """\
apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: request
  displayName: Request
  description: A custom runner action.
spec:
  runtime:
    kind: custom-runner
    entryPoint: custom_runner.actions:Request
  input:
    kind: inline
    value:
      type: object
  output:
    kind: inline
    value:
      type: object
  error:
    kind: inline
    value:
      type: object
""",
        encoding="utf-8",
    )
    documentation = workspace / "docs/README.md"
    documentation.parent.mkdir()
    documentation.write_text("# Custom runner\n", encoding="utf-8")

    assert main(["validate", str(workspace / "plugin.yaml"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["deferredImageBindings"] == ["custom-runner-image"]

    assert (
        main(
            [
                "build",
                str(workspace / "plugin.yaml"),
                "--output",
                str(workspace / "custom-runner.oci.tar"),
                "--json",
            ]
        )
        == 1
    )
    assert "IMAGE_BINDING_MISSING" in capsys.readouterr().err


def test_validate_rejects_malformed_or_repeated_image_binding(tmp_path: Path, capsys) -> None:
    """The CLI never invents or resolves image references."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()

    assert (
        main(["validate", str(workspace / "plugin.yaml"), "--image-binding", "not-a-binding"]) == 2
    )
    assert "must use" in capsys.readouterr().err
    assert (
        main(
            [
                "validate",
                str(workspace / "plugin.yaml"),
                "--image-binding",
                "workload=quay.io/acme/test@sha256:" + "a" * 64,
                "--image-binding",
                "workload=quay.io/acme/other@sha256:" + "b" * 64,
            ]
        )
        == 2
    )
    assert "repeats logical image ID" in capsys.readouterr().err


def test_build_reports_the_immutable_oci_artifact_metadata(tmp_path: Path, capsys) -> None:
    """Build uses the public compiler and OCI builder without registry I/O."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()

    archive = tmp_path / "status-tools.oci.tar"
    assert main(["build", str(workspace / "plugin.yaml"), "--output", str(archive), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["compiledDigest"].startswith("sha256:")
    assert payload["artifactDigest"].startswith("sha256:")
    assert [blob["name"] for blob in payload["blobs"]] == [
        "config",
        "pluginDescriptor",
        "contentBundle",
    ]
    assert payload["archive"] == str(archive)
    assert archive.is_file()


def test_publish_requires_password_stdin(tmp_path: Path, capsys) -> None:
    """The command never accepts a registry password as an argument."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    archive = tmp_path / "status-tools.oci.tar"
    assert main(["build", str(workspace / "plugin.yaml"), "--output", str(archive)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "publish",
                str(archive),
                "--registry-origin",
                "https://registry.example.test",
                "--repository",
                "acme/status-tools",
                "--channel",
                "0.1.0",
                "--username",
                "publisher",
            ]
        )
        == 2
    )
    assert "requires --password-stdin" in capsys.readouterr().err


def test_publish_reports_immutable_reference(tmp_path: Path, capsys, monkeypatch) -> None:
    """Publication accepts only an explicit destination and stdin credential."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    archive = tmp_path / "status-tools.oci.tar"
    assert main(["build", str(workspace / "plugin.yaml"), "--output", str(archive)]) == 0
    capsys.readouterr()

    published: dict[str, object] = {}

    class Publisher:
        def __init__(self, target, *, credentials) -> None:
            published["target"] = target
            published["credentials"] = credentials

        def publish(self, artifact, **kwargs):
            published["artifact"] = artifact
            return SimpleNamespace(
                channel="0.1.0",
                immutable_reference=f"acme/status-tools@{artifact.digest}",
                repository="acme/status-tools",
            )

    monkeypatch.setattr("syntara_plugin.sdk.cli.PluginArtifactPublisher", Publisher)
    monkeypatch.setattr("sys.stdin.read", lambda: "not-printed\n")

    assert (
        main(
            [
                "publish",
                str(archive),
                "--registry-origin",
                "https://registry.example.test",
                "--repository",
                "acme/status-tools",
                "--channel",
                "0.1.0",
                "--username",
                "publisher",
                "--password-stdin",
                "--no-update-catalog",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["immutableReference"] == f"acme/status-tools@{payload['artifactDigest']}"
    assert published["credentials"].password == "not-printed"


def test_build_discovers_cwd_settings_and_command_line_overrides_its_output(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A present CWD settings file wins, while an explicit option wins over it."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    configured_output = tmp_path / "configured.oci.tar"
    explicit_output = tmp_path / "explicit.oci.tar"
    (tmp_path / SETTINGS_FILENAME).write_text(
        "\n".join(
            (
                "apiVersion: syntara.io/v1alpha1",
                "kind: PluginBuildSettings",
                f"manifest: {workspace / 'plugin.yaml'}",
                "build:",
                f"  artifactOutput: {configured_output}",
                "",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert main(["build", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["archive"] == str(configured_output)
    assert configured_output.is_file()

    assert main(["build", "--output", str(explicit_output), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["archive"] == str(explicit_output)
    assert explicit_output.is_file()


def test_build_reads_local_auth_and_output_preferences_from_settings(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Build stays local; publish then uses the configured auth file and defaults."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "auths": {
                    "registry.example.test": {
                        "auth": base64.b64encode(b"publisher:local-password").decode("ascii")
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    settings = workspace / SETTINGS_FILENAME
    settings.write_text(
        "\n".join(
            (
                "apiVersion: syntara.io/v1alpha1",
                "kind: PluginBuildSettings",
                "manifest: plugin.yaml",
                "output:",
                "  verbose: true",
                "  json: true",
                "registry:",
                "  origin: https://registry.example.test",
                "  artifactRepository: acme/plugins/status-tools",
                "  username: publisher",
                "  authFile: ../auth.json",
                "build:",
                "  artifactOutput: dist/status-tools.oci.tar",
                "publish:",
                "  channel: 0.1.0",
                "  updateCatalog: false",
                "",
            )
        ),
        encoding="utf-8",
    )
    received: dict[str, object] = {}

    class Publisher:
        def __init__(self, target, *, credentials):
            received["target"] = target
            received["credentials"] = credentials

        def publish(self, artifact):
            return SimpleNamespace(
                channel="0.1.0",
                immutable_reference=f"acme/plugins/status-tools@{artifact.digest}",
                repository="acme/plugins/status-tools",
            )

    monkeypatch.setattr("syntara_plugin.sdk.cli.PluginArtifactPublisher", Publisher)

    assert main(["build", "--settings", str(settings)]) == 0
    build_output = json.loads(capsys.readouterr().out)
    assert build_output["archive"] == str(workspace / "dist/status-tools.oci.tar")
    assert "ok" not in build_output
    assert main(["publish", build_output["archive"], "--settings", str(settings)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert received["credentials"].username == "publisher"
    assert received["credentials"].password == "local-password"


def test_publish_updates_the_configured_catalog_by_default(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A normal plugin release publishes its discoverability record without another command."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    settings = workspace / SETTINGS_FILENAME
    settings.write_text(
        """apiVersion: syntara.io/v1alpha1
kind: PluginBuildSettings
manifest: plugin.yaml
registry:
  origin: https://registry.example.test
  artifactRepository: acme/plugins/status-tools
  catalogRepository: acme/catalog-index
  username: publisher
build:
  artifactOutput: dist/status-tools.oci.tar
publish:
  channel: 0.1.0
catalog:
  sourceId: public-catalog
  channel: stable
  expiresInHours: 168
""",
        encoding="utf-8",
    )
    published: dict[str, object] = {}

    def publish(arguments, archive):
        published["archive"] = archive
        return {
            "immutableReference": "acme/plugins/status-tools@sha256:" + "a" * 64,
            "repository": "acme/plugins/status-tools",
            "channel": "0.1.0",
        }

    def update_catalog(arguments, *, artifact_path):
        published["catalog_archive"] = artifact_path
        assert arguments.catalog_repository == "acme/catalog-index"
        assert arguments.catalog_source_id == "public-catalog"
        return {
            "immutableReference": "acme/catalog-index@sha256:" + "b" * 64,
            "generation": 1,
        }

    monkeypatch.setattr("syntara_plugin.sdk.cli._publish_archive", publish)
    monkeypatch.setattr("syntara_plugin.sdk.cli._run_catalog_update", update_catalog)

    assert main(["build", "--settings", str(settings), "--json"]) == 0
    build_output = json.loads(capsys.readouterr().out)
    assert build_output["archive"] == str(workspace / "dist/status-tools.oci.tar")
    assert main(["publish", build_output["archive"], "--settings", str(settings), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["catalog"]["generation"] == 1
    assert published["catalog_archive"] == workspace / "dist/status-tools.oci.tar"


def test_settings_rejects_registry_secrets(tmp_path: Path, capsys) -> None:
    """Stable settings reduce repetition but cannot become a secret store."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    settings = tmp_path / SETTINGS_FILENAME
    settings.write_text(
        "\n".join(
            (
                "apiVersion: syntara.io/v1alpha1",
                "kind: PluginBuildSettings",
                "publish:",
                "  password: must-not-be-here",
                "",
            )
        ),
        encoding="utf-8",
    )

    assert main(["build", str(workspace / "plugin.yaml"), "--settings", str(settings)]) == 2
    assert "must not contain secret value" in capsys.readouterr().err


def test_settings_reject_legacy_duplicate_registry_fields(tmp_path: Path, capsys) -> None:
    """Unreleased settings use one registry block instead of compatibility aliases."""
    workspace = _custom_runner_workspace(tmp_path)
    settings = workspace / SETTINGS_FILENAME
    settings.write_text(
        "\n".join(
            (
                "apiVersion: syntara.io/v1alpha1",
                "kind: PluginBuildSettings",
                "manifest: plugin.yaml",
                "registry:",
                "  origin: https://registry.example.test",
                "publish:",
                "  registryOrigin: https://registry.example.test",
                "  channel: 0.1.0",
                "",
            )
        ),
        encoding="utf-8",
    )

    assert main(["build", "--settings", str(settings)]) == 2
    assert "publish contains unsupported setting(s): registryOrigin" in capsys.readouterr().err


def test_settings_rejects_legacy_plugin_repository_field(tmp_path: Path, capsys) -> None:
    """The artifact repository name describes the published OCI object precisely."""
    workspace = _custom_runner_workspace(tmp_path)
    settings = workspace / SETTINGS_FILENAME
    settings.write_text(
        "\n".join(
            (
                "apiVersion: syntara.io/v1alpha1",
                "kind: PluginBuildSettings",
                "manifest: plugin.yaml",
                "registry:",
                "  origin: https://registry.example.test",
                "  pluginRepository: acme/plugins/custom",
                "",
            )
        ),
        encoding="utf-8",
    )

    assert main(["build", "--settings", str(settings)]) == 2
    assert "registry contains unsupported setting(s): pluginRepository" in capsys.readouterr().err


def test_settings_rejects_legacy_build_output_field(tmp_path: Path, capsys) -> None:
    """Unreleased settings use the explicit artifactOutput name without an alias."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    settings = workspace / SETTINGS_FILENAME
    settings.write_text(
        "\n".join(
            (
                "apiVersion: syntara.io/v1alpha1",
                "kind: PluginBuildSettings",
                "manifest: plugin.yaml",
                "build:",
                "  output: dist/status-tools.oci.tar",
                "",
            )
        ),
        encoding="utf-8",
    )

    assert main(["build", "--settings", str(settings)]) == 2
    assert "build contains unsupported setting(s): output" in capsys.readouterr().err


def test_settings_rejects_retired_single_workload_configuration(tmp_path: Path, capsys) -> None:
    """A removed single-image field must fail rather than be silently ignored."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    settings = workspace / SETTINGS_FILENAME
    settings.write_text(
        """apiVersion: syntara.io/v1alpha1
kind: PluginBuildSettings
manifest: plugin.yaml
build:
  workload:
    engine: podman
    context: .
    containerfile: Containerfile
    tag: 0.1.0
    platform: linux/amd64
    imageId: runtime
""",
        encoding="utf-8",
    )

    assert main(["build", "--settings", str(settings)]) == 2
    assert "build contains unsupported setting(s): workload" in capsys.readouterr().err


def test_settings_reject_build_target_names_that_are_not_safe_ids(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "custom-runner"
    assert main(["init", str(workspace), "--template", "custom-workload"]) == 0
    capsys.readouterr()
    settings_path = workspace / SETTINGS_FILENAME
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    settings["build"]["workloads"] = {
        "../outside": {
            "engine": "podman",
            "context": ".",
            "containerfile": "Containerfile",
            "tag": "0.1.0",
            "platform": "linux/amd64",
        }
    }
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")

    assert main(["build", "--settings", str(settings_path)]) == 2
    assert "does not match" in capsys.readouterr().err


def test_build_creates_local_plugin_and_binding_release_from_source_manifest(
    tmp_path: Path, capsys
) -> None:
    """The build command resolves immutable image references without publishing anything."""
    workspace = tmp_path / "http-plugin"
    assert main(["init", str(workspace), "--namespace", "acme", "--template", "custom-workload"]) == 0
    capsys.readouterr()
    binding_path = workspace / "runtime-bindings.yaml"
    binding = yaml.safe_load(binding_path.read_text(encoding="utf-8"))
    binding["spec"]["workloads"]["python"]["options"][0]["image"] = {
        "reference": f"quay.io/acme/python-runtime@{_WORKLOAD_DIGEST}"
    }
    binding_path.write_text(yaml.safe_dump(binding, sort_keys=False), encoding="utf-8")
    settings = workspace / SETTINGS_FILENAME
    settings_document = yaml.safe_load(settings.read_text(encoding="utf-8"))
    settings_document["registry"].update(
        {
            "origin": "https://quay.io",
            "artifactRepository": "acme/http-plugin",
            "workloadRepository": "acme/http-plugin-runtime",
        }
    )
    settings.write_text(yaml.safe_dump(settings_document, sort_keys=False), encoding="utf-8")

    assert main(["build", "--settings", str(settings), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    release_path = workspace / "dist/release.json"
    assert payload["ok"] is True
    assert payload["images"] == []
    assert Path(payload["pluginArchive"]).is_file()
    assert Path(payload["bindingArchive"]).is_file()
    assert Path(payload["bindingDocument"]).is_file()
    assert Path(payload["releaseInventory"]) == release_path
    release = json.loads(release_path.read_text(encoding="utf-8"))
    assert release["plugin"]["publish"] is True
    assert release["plugin"]["repository"] == "acme/http-plugin"
    assert release["runtimeBinding"]["repository"] == "acme/http-plugin"
    assert release["plugin"]["archive"] == "http-plugin.oci.tar"
    assert release["runtimeBinding"]["archive"].startswith("runtime-bindings-")
    assert not Path(release["plugin"]["archive"]).is_absolute()
    assert not Path(release["runtimeBinding"]["archive"]).is_absolute()
    resolved = yaml.safe_load(Path(payload["bindingDocument"]).read_text(encoding="utf-8"))
    assert resolved["spec"]["workloads"]["python"]["options"][0]["image"].endswith(
        _WORKLOAD_DIGEST
    )
    assert main(["inspect", payload["bindingDocument"], "--json"]) == 0
    inspected_binding = json.loads(capsys.readouterr().out)
    assert inspected_binding["kind"] == "RuntimeBinding"
    assert inspected_binding["valid"] is True

    assert main(["build", "--settings", str(settings), "--json"]) == 0
    repeated_build = json.loads(capsys.readouterr().out)
    assert repeated_build["pluginArtifactDigest"] == payload["pluginArtifactDigest"]
    assert repeated_build["bindingArtifactDigest"] == payload["bindingArtifactDigest"]

    outside_archive = tmp_path / "outside.oci.tar"
    assert (
        main(
            [
                "build",
                "--settings",
                str(settings),
                "--output",
                str(outside_archive),
            ]
        )
        == 1
    )
    assert "must be under" in capsys.readouterr().err
    assert not outside_archive.exists()


def test_image_only_release_keeps_plugin_and_catalog_unchanged(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    workspace = tmp_path / "image-only-plugin"
    assert main(
        ["init", str(workspace), "--namespace", "acme", "--template", "custom-workload"]
    ) == 0
    capsys.readouterr()
    settings_path = workspace / SETTINGS_FILENAME
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    settings["registry"].update(
        {
            "origin": "https://registry.example.test",
            "artifactRepository": "acme/plugins/image-only",
            "workloadRepository": "acme/workloads/image-only",
            "catalogRepository": "acme/catalog-index",
            "username": "publisher",
        }
    )
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")

    image_digest = "sha256:" + "c" * 64
    image_reference = f"acme/workloads/image-only@{image_digest}"
    image_archive = workspace / "dist" / f"python-runtime-{image_digest[7:]}.oci.tar"
    image_archive.parent.mkdir(parents=True, exist_ok=True)
    image_archive.write_bytes(b"test image archive placeholder")

    def build_images(*args, **kwargs):
        return [
            {
                "archive": str(image_archive),
                "buildTarget": "python-runtime",
                "digest": image_digest,
                "platform": "linux/amd64",
                "reference": image_reference,
                "repository": "acme/workloads/image-only",
                "tag": "1.0.0",
                "workload": "python",
            }
        ]

    monkeypatch.setattr("syntara_plugin.sdk.cli._build_runtime_binding_images", build_images)
    assert main(["build", "--settings", str(settings_path), "--json"]) == 0
    first_build = json.loads(capsys.readouterr().out)
    plugin_archive = Path(first_build["pluginArchive"])
    plugin_digest = first_build["pluginArtifactDigest"]

    assert (
        main(
            [
                "build",
                "--settings",
                str(settings_path),
                "--plugin-artifact-archive",
                str(plugin_archive),
                "--json",
            ]
        )
        == 0
    )
    image_build = json.loads(capsys.readouterr().out)
    inventory_path = Path(image_build["releaseInventory"])
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert inventory["plugin"]["publish"] is False
    assert inventory["plugin"]["digest"] == plugin_digest

    class _BindingPublisher:
        def __init__(self, target, *, credentials=None):
            self.target = target

        def publish(self, artifact, **kwargs):
            return SimpleNamespace(
                repository=self.target.repository,
                channel=self.target.channel,
                manifest_digest=artifact.digest,
                immutable_reference=f"{self.target.repository}@{artifact.digest}",
            )

    class _IndexPublisher(_BindingPublisher):
        def __init__(self, target, *, credentials=None, verify_current_index=None, allow_unverified_current=False):
            super().__init__(target, credentials=credentials)

        def read_current(self):
            return None

    monkeypatch.setattr("syntara_plugin.sdk.cli._publication_credentials", lambda *a, **k: RegistryCredentials("publisher", "pw"))
    monkeypatch.setattr("syntara_plugin.sdk.cli._temporary_skopeo_auth_file", lambda *a, **k: nullcontext(None))
    monkeypatch.setattr("syntara_plugin.sdk.cli.RuntimeBindingArtifactPublisher", _BindingPublisher)
    monkeypatch.setattr("syntara_plugin.sdk.cli.RuntimeBindingIndexPublisher", _IndexPublisher)
    monkeypatch.setattr("syntara_plugin.sdk.cli.workload_archive_digest", lambda path: image_digest)
    monkeypatch.setattr("syntara_plugin.sdk.cli.publish_workload_archive", lambda archive, destination, **kwargs: image_reference)
    monkeypatch.setattr("syntara_plugin.sdk.cli._sign_reference", lambda *a, **k: False)
    monkeypatch.setattr("syntara_plugin.sdk.cli._publish_archive", lambda *a, **k: pytest.fail("image-only update must not republish P"))
    monkeypatch.setattr("syntara_plugin.sdk.cli._run_catalog_update", lambda *a, **k: pytest.fail("image-only update must not update the plugin catalog"))
    arguments = SimpleNamespace(
        registry_origin="https://registry.example.test",
        username="publisher",
        repository="acme/plugins/image-only",
        channel="1.0.0",
        expires_in_hours=24,
        allow_insecure_loopback_http=False,
        update_catalog=True,
        expected_binding_index_digest=None,
        cosign_verify_key=None,
        allow_unverified_binding_index=False,
        cosign_key=None,
        verbose=False,
    )

    result = _publish_release(arguments, inventory_path)

    assert result["ok"] is True
    assert "catalog" not in result
    assert not any(publication["kind"] == "plugin" for publication in result["publications"])
    assert any(publication["kind"] == "workload-image" for publication in result["publications"])


def test_runtime_binding_image_archives_are_content_addressed_and_reusable(
    tmp_path: Path, monkeypatch
) -> None:
    descriptor = json.loads(
        (Path(__file__).parents[4] / "contracts/fixtures/plugin-descriptor.valid.json").read_text()
    )
    source = {
        "spec": {
            "workloads": {
                "plugin-workload": {
                    "options": [{"name": "default", "image": {"buildTarget": "python-runtime"}}]
                }
            }
        }
    }
    containerfile = tmp_path / "Containerfile"
    containerfile.write_text("FROM scratch\n", encoding="utf-8")
    settings = SimpleNamespace(
        engine="podman",
        context=tmp_path,
        containerfile=containerfile,
        repository="registry.example.test/acme/plugin-runtime",
        tag="0.1.0",
        platform="linux/amd64",
    )
    arguments = SimpleNamespace(
        workload_targets={"python-runtime": settings},
        registry_origin="https://registry.example.test",
        workload_repository=None,
        allow_insecure_loopback_http=False,
        registry_auth_file=None,
        verbose=False,
    )
    expected_digest = "sha256:" + "e" * 64
    built_paths: list[Path] = []

    def build(_request, destination, *, verbose):
        built_paths.append(destination)
        destination.write_bytes(b"image archive")
        return f"registry.example.test/acme/plugin-runtime@{expected_digest}"

    monkeypatch.setattr("syntara_plugin.sdk.cli.build_workload_archive", build)
    monkeypatch.setattr(
        "syntara_plugin.sdk.cli.workload_archive_digest", lambda _path: expected_digest
    )

    first = _build_runtime_binding_images(source, descriptor, arguments, dist_directory=tmp_path / "dist")
    second = _build_runtime_binding_images(source, descriptor, arguments, dist_directory=tmp_path / "dist")

    assert len(built_paths) == 2
    assert built_paths[0] != built_paths[1]
    assert first[0]["archive"] == second[0]["archive"]
    assert Path(first[0]["archive"]).name == f"python-runtime-{'e' * 64}.oci.tar"
    assert Path(first[0]["archive"]).read_bytes() == b"image archive"


def test_release_inventory_rejects_oversized_document(tmp_path: Path) -> None:
    inventory = tmp_path / "dist/release.json"
    inventory.parent.mkdir()
    inventory.write_bytes(b"{" + b" " * 1_048_576)

    with pytest.raises(SettingsError, match="1 MiB safety limit"):
        _read_release_inventory(inventory)


def test_release_inventory_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    inventory = tmp_path / "dist/release.json"
    inventory.parent.mkdir()
    inventory.write_text('{"kind":"wrong","kind":"PluginReleaseInventory"}', encoding="utf-8")

    with pytest.raises(SettingsError, match="duplicate JSON object key"):
        _read_release_inventory(inventory)


def test_release_inventory_rejects_traversal_and_symlink_archive_paths(tmp_path: Path) -> None:
    inventory = tmp_path / "dist/release.json"
    inventory.parent.mkdir()
    archive = inventory.parent / "plugin.oci.tar"
    archive.write_bytes(b"archive")
    outside = tmp_path / "outside.oci.tar"
    outside.write_bytes(b"outside")

    with pytest.raises(SettingsError, match="relative and stay under"):
        _release_inventory_path(inventory, "../outside.oci.tar", "plugin archive")

    symlink = inventory.parent / "linked.oci.tar"
    symlink.symlink_to(outside)
    with pytest.raises(SettingsError, match="must not traverse symlinks"):
        _release_inventory_path(inventory, "linked.oci.tar", "plugin archive")

    assert _release_inventory_path(inventory, "plugin.oci.tar", "plugin archive") == archive


def test_build_validates_plugin_before_building_workload_images(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Invalid plugin source fails before the local container engine is called."""
    workspace = tmp_path / "custom-runner"
    assert main(["init", str(workspace), "--namespace", "acme", "--template", "custom-workload"]) == 0
    capsys.readouterr()
    action_path = workspace / "steps/request/manifest.yaml"
    action = yaml.safe_load(action_path.read_text(encoding="utf-8"))
    action["spec"].pop("output")
    action_path.write_text(yaml.safe_dump(action, sort_keys=False), encoding="utf-8")
    calls: list[object] = []
    monkeypatch.setattr(
        "syntara_plugin.sdk.cli.build_workload_archive",
        lambda request, destination, *, verbose: calls.append(request),
    )

    assert main(["build", "--settings", str(workspace / SETTINGS_FILENAME)]) == 1
    assert calls == []
    assert capsys.readouterr().err


def test_catalog_update_derives_and_publishes_a_catalog_entry_from_a_verified_archive(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    workspace = tmp_path / "status-tools"
    archive = tmp_path / "dist/status-tools.oci.tar"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    assert main(["build", str(workspace / "plugin.yaml"), "--output", str(archive)]) == 0
    capsys.readouterr()
    settings = tmp_path / SETTINGS_FILENAME
    settings.write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: PluginBuildSettings
registry:
  origin: https://registry.example.test
  artifactRepository: acme/plugins/status-tools
  catalogRepository: acme/catalog-index
  username: publisher
publish:
  artifact: {archive}
catalog:
  sourceId: public-catalog
  channel: stable
  expiresInHours: 168
""",
        encoding="utf-8",
    )
    published: dict[str, object] = {}

    class Publisher:
        def __init__(self, target, *, credentials) -> None:
            published["target"] = target
            published["credentials"] = credentials

        def read_current(self):
            return None

        def publish(self, artifact):
            published["artifact"] = artifact
            return SimpleNamespace(
                channel="stable",
                immutable_reference=f"acme/catalog-index@{artifact.digest}",
                manifest_digest=artifact.digest,
                repository="acme/catalog-index",
            )

    monkeypatch.setattr("syntara_plugin.sdk.cli.CatalogIndexPublisher", Publisher)
    monkeypatch.setattr("sys.stdin.read", lambda: "not-printed\n")

    assert (
        main(
            [
                "catalog",
                "update",
                "--settings",
                str(settings),
                "--password-stdin",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["idempotent"] is False
    assert payload["generation"] == 1
    assert payload["entry"] == {
        "namespace": "example",
        "name": "status-tools",
        "version": "0.1.0",
        "artifactRepository": "acme/plugins/status-tools",
        "artifactDigest": payload["entry"]["artifactDigest"],
        "documentationDigest": payload["entry"]["documentationDigest"],
        "runtimeOwnerships": ["platform-runner"],
    }
    assert payload["entry"]["artifactDigest"].startswith("sha256:")
    assert payload["entry"]["documentationDigest"].startswith("sha256:")
    assert json.loads(published["artifact"].catalog_index.content)["spec"]["entries"] == [
        payload["entry"]
    ]
    assert published["target"].repository == "acme/catalog-index"
    assert published["credentials"].password == "not-printed"


def test_catalog_recover_continues_from_the_explicit_accepted_index(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """A lost mutable tag is restored from a verified immutable predecessor."""
    workspace = tmp_path / "status-tools"
    archive = tmp_path / "dist/status-tools.oci.tar"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    assert main(["build", str(workspace / "plugin.yaml"), "--output", str(archive)]) == 0
    capsys.readouterr()
    settings = tmp_path / SETTINGS_FILENAME
    settings.write_text(
        f"""apiVersion: syntara.io/v1alpha1
kind: PluginBuildSettings
registry:
  origin: https://registry.example.test
  artifactRepository: acme/plugins/status-tools
  catalogRepository: acme/catalog-index
  username: publisher
publish:
  artifact: {archive}
catalog:
  sourceId: public-catalog
  channel: stable
  expiresInHours: 168
""",
        encoding="utf-8",
    )
    previous_digest = "sha256:" + "f" * 64
    previous_document = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "CatalogIndex",
        "metadata": {
            "sourceId": "public-catalog",
            "generation": 2,
            "issuedAt": "2026-10-01T12:00:00Z",
            "expiresAt": "2026-10-08T12:00:00Z",
            "previousIndexDigest": "sha256:" + "e" * 64,
        },
        "spec": {
            "contractVersion": "syntara.io/v1alpha1",
            "entries": [
                {
                    "namespace": "example",
                    "name": "existing",
                    "version": "0.1.0",
                    "artifactRepository": "acme/plugins/existing",
                    "artifactDigest": "sha256:" + "a" * 64,
                    "documentationDigest": "sha256:" + "b" * 64,
                    "runtimeOwnerships": ["platform-runner"],
                }
            ],
        },
    }
    published: dict[str, object] = {}

    class Publisher:
        def __init__(self, target, *, credentials) -> None:
            published["target"] = target
            published["credentials"] = credentials

        def read_immutable(self, digest):
            assert digest == previous_digest
            return SimpleNamespace(manifest_digest=digest, document=previous_document)

        def publish(self, artifact):
            published["artifact"] = artifact
            return SimpleNamespace(
                channel="stable",
                immutable_reference=f"acme/catalog-index@{artifact.digest}",
                manifest_digest=artifact.digest,
                repository="acme/catalog-index",
            )

    monkeypatch.setattr("syntara_plugin.sdk.cli.CatalogIndexPublisher", Publisher)
    monkeypatch.setattr("sys.stdin.read", lambda: "not-printed\n")

    assert (
        main(
            [
                "catalog",
                "recover",
                "--settings",
                str(settings),
                "--previous-index-digest",
                previous_digest,
                "--password-stdin",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    recovered = json.loads(published["artifact"].catalog_index.content)
    assert payload["recovered"] is True
    assert payload["generation"] == 3
    assert payload["previousIndexDigest"] == previous_digest
    assert recovered["metadata"]["previousIndexDigest"] == previous_digest
    assert [entry["name"] for entry in recovered["spec"]["entries"]] == [
        "existing",
        "status-tools",
    ]


def test_catalog_entry_records_all_runtime_ownerships() -> None:
    """Every target runtime contributes ownership, regardless of concrete kind."""
    descriptor = {
        "metadata": {"namespace": "example", "name": "mixed", "version": "0.1.0"},
        "spec": {
            "documentation": {"digest": "sha256:" + "d" * 64},
            "targets": [
                {"kind": "IntegrationType", "identity": "example.connection"},
                {"kind": "CredentialType", "identity": "example.token"},
                {"kind": "Action", "runtime": {"kind": "custom-runner"}},
                {"kind": "Action", "runtime": {"kind": "platform-runner"}},
            ],
        },
    }
    artifact = SimpleNamespace(
        digest="sha256:" + "a" * 64,
        plugin_manifest=SimpleNamespace(content=json.dumps(descriptor).encode("utf-8")),
    )

    entry = _catalog_entry(artifact, "acme/plugins/mixed")

    assert entry["runtimeOwnerships"] == ["custom-runner", "platform-runner"]


@pytest.mark.parametrize(
    ("kind", "runtime_kind"),
    [("Control", "custom-runner"), ("Trigger", "platform-runner")],
)
def test_catalog_entry_includes_control_and_trigger_runtime_ownership(
    kind: str, runtime_kind: str
) -> None:
    descriptor = {
        "metadata": {"namespace": "example", "name": "mixed", "version": "0.1.0"},
        "spec": {
            "documentation": {"digest": "sha256:" + "d" * 64},
            "targets": [
                {"kind": kind, "identity": "example.step", "runtime": {"kind": runtime_kind}}
            ],
        },
    }
    artifact = SimpleNamespace(
        digest="sha256:" + "a" * 64,
        plugin_manifest=SimpleNamespace(content=json.dumps(descriptor).encode("utf-8")),
    )

    entry = _catalog_entry(artifact, "acme/plugins/mixed")

    assert entry["runtimeOwnerships"] == [runtime_kind]


@pytest.mark.parametrize("kind", ["Control", "Trigger"])
def test_catalog_entry_supports_plugins_without_runner_targets(kind: str) -> None:
    descriptor = {
        "metadata": {"namespace": "example", "name": "control-only", "version": "0.1.0"},
        "spec": {
            "documentation": {"digest": "sha256:" + "d" * 64},
            "targets": [{"kind": kind, "identity": "example.step"}],
        },
    }
    artifact = SimpleNamespace(
        digest="sha256:" + "a" * 64,
        plugin_manifest=SimpleNamespace(content=json.dumps(descriptor).encode("utf-8")),
    )

    entry = _catalog_entry(artifact, "acme/plugins/control-only")

    assert entry["runtimeOwnerships"] == []


def _custom_runner_workspace(tmp_path: Path) -> Path:
    """Write the smallest source workspace that requires one custom runner image."""
    workspace = tmp_path / "custom-runner"
    (workspace / "steps/create/manifest.yaml").parent.mkdir(parents=True)
    (workspace / "docs").mkdir()
    (workspace / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
    (workspace / "plugin.yaml").write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: acme
  name: custom-runner
  version: 0.1.0
  displayName: Custom runner
  description: One custom runner action.
spec:
  documentation:
    path: docs/README.md
  workload:
    image: custom-runner
  targets:
    - steps/create/manifest.yaml
""",
        encoding="utf-8",
    )
    (workspace / "steps/create/manifest.yaml").write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Action
metadata:
  name: create
  displayName: Create
  description: A custom runner action.
spec:
  runtime:
    kind: custom-runner
    entryPoint: custom_runner.actions:Create
  input:
    kind: inline
    value:
      type: object
  output:
    kind: inline
    value:
      type: object
  error:
    kind: inline
    value:
      type: object
""",
        encoding="utf-8",
    )
    (workspace / "docs/README.md").write_text("# Custom runner\n", encoding="utf-8")
    return workspace
