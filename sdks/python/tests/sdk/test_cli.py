"""End-to-end checks for the thin offline authoring CLI."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

from syntara_plugin.sdk.cli import _credentials_from_auth_file, main
from syntara_plugin.sdk.settings import SETTINGS_FILENAME


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

    assert main(["validate", str(workspace / "plugin.yaml"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert payload["ok"] is True
    assert payload["digest"].startswith("sha256:")
    assert payload["assetCount"] == 2


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
    assert descriptor["spec"]["targets"][0]["runtime"]["driver"] == "http.v1"


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


def test_validate_defers_a_custom_workload_image_binding_until_build(
    tmp_path: Path, capsys
) -> None:
    """Source validation does not need an image that has not been built yet."""
    workspace = tmp_path / "custom-workload"
    workspace.mkdir()
    (workspace / "plugin.yaml").write_text(
        """\
apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: acme
  name: custom-workload
  version: 0.1.0
  displayName: Custom workload
  description: A custom workload plugin.
spec:
  workload:
    image: custom-workload-image
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
  description: A custom workload action.
spec:
  runtime:
    kind: custom-workload
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

    assert main(["validate", str(workspace / "plugin.yaml"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["deferredImageBindings"] == ["custom-workload-image"]

    assert (
        main(
            [
                "build",
                str(workspace / "plugin.yaml"),
                "--output",
                str(workspace / "custom-workload.oci.tar"),
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

        def publish(self, artifact):
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
    """Local release defaults remove repetitive developer command options."""
    workspace = tmp_path / "status-tools"
    assert main(["init", str(workspace)]) == 0
    capsys.readouterr()
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"auths": {}}', encoding="utf-8")
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
                "  authFile: ../auth.json",
                "build:",
                "  artifactOutput: dist/status-tools.oci.tar",
                "publish:",
                "  channel: 0.1.0",
                "",
            )
        ),
        encoding="utf-8",
    )
    received: dict[str, object] = {}

    def publish(arguments, archive):
        received["auth_file"] = arguments.registry_auth_file
        received["verbose"] = arguments.verbose
        return {"immutableReference": "acme/plugins/status-tools@sha256:" + "a" * 64}

    monkeypatch.setattr("syntara_plugin.sdk.cli._publish_archive", publish)

    assert main(["build", "--settings", str(settings), "--publish"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert received["auth_file"] == auth_file
    assert received["verbose"] is True


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
    workspace = _custom_workload_workspace(tmp_path)
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
    workspace = _custom_workload_workspace(tmp_path)
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


def test_build_with_workload_binds_the_pushed_digest_and_publishes_metadata(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """One opt-in command retains both OCI outputs and never stores a password."""
    workspace = _custom_workload_workspace(tmp_path)
    settings = workspace / SETTINGS_FILENAME
    settings.write_text(
        "\n".join(
            (
                "apiVersion: syntara.io/v1alpha1",
                "kind: PluginBuildSettings",
                "manifest: plugin.yaml",
                "registry:",
                "  origin: https://registry.example.test",
                "  artifactRepository: acme/plugins/custom",
                "  workloadRepository: acme/custom-workload",
                "  username: publisher",
                "build:",
                "  artifactOutput: dist/custom.oci.tar",
                "  workload:",
                "    engine: podman",
                "    context: .",
                "    containerfile: Containerfile",
                "    tag: 0.1.0",
                "    platform: linux/amd64",
                "    imageId: custom-workload",
                "publish:",
                "  channel: 0.1.0",
                "",
            )
        ),
        encoding="utf-8",
    )
    built: dict[str, object] = {}

    def build_workload(request, *, verbose):
        built["request"] = request
        built["verbose"] = verbose
        return f"registry.example.test/acme/custom-workload@{_WORKLOAD_DIGEST}"

    class Publisher:
        def __init__(self, target, *, credentials) -> None:
            built["target"] = target
            built["credentials"] = credentials

        def publish(self, artifact):
            built["artifact"] = artifact
            return SimpleNamespace(
                channel="0.1.0",
                immutable_reference=f"acme/plugins/custom@{artifact.digest}",
                repository="acme/plugins/custom",
            )

    monkeypatch.setattr("syntara_plugin.sdk.cli.build_and_push_workload", build_workload)
    monkeypatch.setattr("syntara_plugin.sdk.cli.PluginArtifactPublisher", Publisher)
    monkeypatch.setattr("sys.stdin.read", lambda: "not-printed\n")

    assert (
        main(
            [
                "build",
                "--settings",
                str(settings),
                "--with-workload",
                "--publish",
                "--password-stdin",
                "--verbose",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert (
        payload["workloadImage"] == f"registry.example.test/acme/custom-workload@{_WORKLOAD_DIGEST}"
    )
    assert payload["workloadPlatform"] == "linux/amd64"
    assert payload["workloadRepository"] == "registry.example.test/acme/custom-workload"
    assert payload["publication"]["immutableReference"] == (
        f"acme/plugins/custom@{payload['artifactDigest']}"
    )
    assert payload["archive"] == str(workspace / "dist/custom.oci.tar")
    assert built["credentials"].password == "not-printed"
    assert built["request"].platform == "linux/amd64"
    descriptor = json.loads(built["artifact"].plugin_manifest.content)
    assert descriptor["spec"]["workloads"]["custom-workload"]["image"] == (
        f"registry.example.test/acme/custom-workload@{_WORKLOAD_DIGEST}"
    )


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
        "runtimeOwnership": "platform-runner",
    }
    assert payload["entry"]["artifactDigest"].startswith("sha256:")
    assert payload["entry"]["documentationDigest"].startswith("sha256:")
    assert json.loads(published["artifact"].catalog_index.content)["spec"]["entries"] == [
        payload["entry"]
    ]
    assert published["target"].repository == "acme/catalog-index"
    assert published["credentials"].password == "not-printed"


def _custom_workload_workspace(tmp_path: Path) -> Path:
    """Write the smallest source workspace that requires one custom workload image."""
    workspace = tmp_path / "custom-workload"
    (workspace / "steps/create/manifest.yaml").parent.mkdir(parents=True)
    (workspace / "docs").mkdir()
    (workspace / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
    (workspace / "plugin.yaml").write_text(
        """apiVersion: syntara.io/v1alpha1
kind: Plugin
metadata:
  namespace: acme
  name: custom-workload
  version: 0.1.0
  displayName: Custom workload
  description: One custom workload action.
spec:
  documentation:
    path: docs/README.md
  workload:
    image: custom-workload
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
  description: A custom workload action.
spec:
  runtime:
    kind: custom-workload
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
    (workspace / "docs/README.md").write_text("# Custom workload\n", encoding="utf-8")
    return workspace
