"""Developer-oriented command-line adapter for the Syntara plugin SDK.

The CLI delegates every source interpretation to ``compile_workspace``. Its
default build stays offline and deterministic. An explicit custom-runner
preview may use Podman to build and push one author-owned image before binding
the registry-returned immutable digest into the normal metadata artifact.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from jsonschema import Draft202012Validator, SchemaError

from .artifact import ArtifactBuildError, PluginArtifact, build_plugin_artifact
from .artifact_archive import (
    ArtifactArchiveError,
    read_plugin_artifact_archive,
    write_plugin_artifact_archive,
)
from .authoring import BuildRequest, CompilationError, CompilationResult, compile_workspace
from .catalog import (
    CatalogIndexArtifactBuildError,
    CatalogIndexUpdateError,
    build_catalog_index_artifact,
    merge_catalog_entry,
    recover_catalog_entry,
)
from .registry import (
    CatalogIndexPublicationError,
    CatalogIndexPublicationTarget,
    CatalogIndexPublisher,
    PluginArtifactPublicationTarget,
    PluginArtifactPublisher,
    RegistryCredentials,
)
from .settings import (
    SETTINGS_FILENAME,
    BuildSettings,
    PluginBuildSettings,
    RegistrySettings,
    SettingsError,
    WorkloadBuildSettings,
    load_settings,
)
from .testgen import ScaffoldError, generate_step_tests
from .workload import WorkloadBuildError, WorkloadBuildRequest, build_and_push_workload

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_FIELD_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MACHINE_READABLE_OUTPUT_HELP = "Print machine-readable output."


def main(argv: Sequence[str] | None = None) -> int:
    """Run the authoring CLI and return its process status."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "init":
        return _init_workspace(arguments)
    if arguments.command == "test-init":
        return _init_tests(arguments)
    try:
        settings = load_settings(
            explicit_path=getattr(arguments, "settings", None),
            manifest=getattr(arguments, "manifest", None),
        )
        _resolve_arguments(arguments, settings)
    except (SettingsError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if arguments.command == "validate":
        return _compile_workspace(arguments)
    if arguments.command == "inspect":
        return _inspect(arguments)
    if arguments.command == "build":
        return _build_artifact(arguments)
    if arguments.command == "publish":
        return _publish_artifact(arguments)
    if arguments.command == "catalog" and arguments.catalog_command == "update":
        return _update_catalog(arguments)
    if arguments.command == "catalog" and arguments.catalog_command == "recover":
        return _recover_catalog(arguments)
    parser.error("a command is required")
    return 2


def _parser() -> argparse.ArgumentParser:
    """Build the stable, dependency-free command surface."""
    parser = argparse.ArgumentParser(
        prog="syntara-plugin", description="Author, build, and explicitly publish Syntara plugins."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser(
        "init", help="Create one safe, compilable HTTP-action plugin workspace."
    )
    init.add_argument("directory", type=Path, help="New empty directory for the plugin workspace.")
    init.add_argument("--namespace", default="example", help="Plugin namespace (default: example).")
    init.add_argument("--name", help="Plugin name (default: final directory name).")
    init.add_argument("--version", default="0.1.0", help="Plugin SemVer version (default: 0.1.0).")
    init.add_argument("--action", default="request", help="Initial action name (default: request).")
    init.add_argument(
        "--fields",
        metavar="FIELDS",
        help=(
            "Comma-separated names, JSON schema, JSON field map, or JSON file path. "
            "File paths must not contain '..' and must resolve inside the working directory."
        ),
    )

    test_init = commands.add_parser(
        "test-init",
        help="Generate local pytest tests for custom-runner Step types.",
    )
    test_init.add_argument(
        "manifest",
        type=Path,
        nargs="?",
        default=Path("plugin.yaml"),
        help="Path to plugin.yaml or its directory (default: ./plugin.yaml).",
    )
    test_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing generated test files.",
    )
    test_init.add_argument(
        "--tests-dir",
        type=Path,
        default=None,
        help="Tests directory (default: <workspace>/tests).",
    )

    for command, help_text in (
        ("validate", "Compile and validate a plugin workspace."),
        ("inspect", "Inspect a plugin workspace or verified OCI plugin archive."),
        ("build", "Build and write a portable OCI plugin archive without publishing it."),
    ):
        compile_command = commands.add_parser(command, help=help_text)
        compile_command.add_argument(
            "manifest",
            type=Path,
            nargs="?",
            help="Path to plugin.yaml; may be supplied by settings.",
        )
        _add_settings_argument(compile_command)
        compile_command.add_argument(
            "--image-binding",
            action="append",
            default=None,
            metavar="ID=REPOSITORY@sha256:DIGEST",
            help="Immutable custom-runner image binding; repeat for each logical image ID.",
        )
        compile_command.add_argument(
            "--json", action="store_true", default=None, help=_MACHINE_READABLE_OUTPUT_HELP
        )
        if command == "build":
            compile_command.add_argument(
                "--output", type=Path, help="New OCI-layout .oci.tar output file."
            )
            compile_command.add_argument(
                "--with-workload",
                action="store_true",
                help="Build and push the configured custom-runner image through Podman.",
            )
            compile_command.add_argument(
                "--publish",
                action="store_true",
                help="Publish the newly written metadata archive and, by default, update its catalog.",
            )
            compile_command.add_argument(
                "--workload-engine", help="Container engine for --with-workload (Podman only)."
            )
            compile_command.add_argument(
                "--workload-context", type=Path, help="Container build context for --with-workload."
            )
            compile_command.add_argument(
                "--workload-containerfile",
                type=Path,
                help="Containerfile path for --with-workload.",
            )
            compile_command.add_argument(
                "--workload-repository",
                help="Full workload-image repository for --with-workload; overrides registry.workloadRepository.",
            )
            compile_command.add_argument(
                "--workload-tag", help="Workload-image tag to build and push."
            )
            compile_command.add_argument(
                "--workload-platform", help="Explicit target platform, for example linux/amd64."
            )
            compile_command.add_argument(
                "--workload-image-id",
                help="Logical manifest workload ID to bind to the pushed image digest.",
            )
            _add_publication_arguments(compile_command)
            _add_catalog_publication_arguments(compile_command)
            compile_command.add_argument(
                "--verbose",
                action="store_true",
                default=None,
                help="Show Podman command lines and delegated build output.",
            )

    publish = commands.add_parser(
        "publish",
        help="Publish one prebuilt OCI plugin archive to an explicit repository.",
    )
    publish.add_argument(
        "artifact", type=Path, nargs="?", help="OCI-layout .oci.tar file produced by build."
    )
    _add_settings_argument(publish)
    _add_publication_arguments(publish)
    _add_catalog_publication_arguments(publish)
    publish.add_argument(
        "--verbose",
        action="store_true",
        default=None,
        help="Show concise publication phase information.",
    )
    publish.add_argument(
        "--json", action="store_true", default=None, help=_MACHINE_READABLE_OUTPUT_HELP
    )

    catalog = commands.add_parser(
        "catalog",
        help="Build and publish the next immutable catalog index from a published plugin artifact.",
    )
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)
    update = catalog_commands.add_parser(
        "update",
        help="Append a released plugin artifact to a catalog index, creating the first index if absent.",
    )
    update.add_argument(
        "artifact", type=Path, nargs="?", help="Published OCI-layout .oci.tar plugin artifact."
    )
    _add_settings_argument(update)
    update.add_argument(
        "--catalog-source-id", help="Catalog source ID; overrides catalog.sourceId."
    )
    update.add_argument(
        "--catalog-repository",
        help="Catalog OCI repository path; overrides registry.catalogRepository.",
    )
    update.add_argument("--catalog-channel", help="Catalog OCI tag; overrides catalog.channel.")
    update.add_argument(
        "--expires-in-hours",
        type=int,
        help="Catalog validity period; overrides catalog.expiresInHours.",
    )
    update.add_argument(
        "--expected-index-digest",
        help="Optional current stable manifest digest to reject a stale release coordinator.",
    )
    update.add_argument(
        "--renew",
        action="store_true",
        help="Publish a successor generation for an unchanged entry to renew catalog expiry.",
    )
    update.add_argument(
        "--registry-origin", help="Registry origin, including scheme; overrides registry.origin."
    )
    update.add_argument("--username", help="Registry username; overrides registry.username.")
    _add_registry_authentication_arguments(update, operation="catalog update")
    update.add_argument(
        "--allow-insecure-loopback-http",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Allow HTTP only for localhost or 127.0.0.1 developer registries; overrides registry.allowInsecureLoopbackHttp.",
    )
    update.add_argument(
        "--verbose", action="store_true", default=None, help="Show concise catalog phases."
    )
    update.add_argument(
        "--json", action="store_true", default=None, help=_MACHINE_READABLE_OUTPUT_HELP
    )
    recover = catalog_commands.add_parser(
        "recover",
        help="Restore a lost catalog channel from Syntara's last accepted immutable index.",
    )
    recover.add_argument(
        "artifact", type=Path, nargs="?", help="Published OCI-layout .oci.tar plugin artifact."
    )
    _add_settings_argument(recover)
    recover.add_argument(
        "--previous-index-digest",
        required=True,
        help="Immutable digest of the catalog index last accepted by Syntara.",
    )
    recover.add_argument(
        "--catalog-source-id", help="Catalog source ID; overrides catalog.sourceId."
    )
    recover.add_argument(
        "--catalog-repository",
        help="Catalog OCI repository path; overrides registry.catalogRepository.",
    )
    recover.add_argument("--catalog-channel", help="Catalog OCI tag; overrides catalog.channel.")
    recover.add_argument(
        "--expires-in-hours",
        type=int,
        help="Catalog validity period; overrides catalog.expiresInHours.",
    )
    recover.add_argument(
        "--registry-origin", help="Registry origin, including scheme; overrides registry.origin."
    )
    recover.add_argument("--username", help="Registry username; overrides registry.username.")
    _add_registry_authentication_arguments(recover, operation="catalog recovery")
    recover.add_argument(
        "--allow-insecure-loopback-http",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Allow HTTP only for localhost or 127.0.0.1 developer registries; overrides registry.allowInsecureLoopbackHttp.",
    )
    recover.add_argument(
        "--verbose", action="store_true", default=None, help="Show concise catalog phases."
    )
    recover.add_argument(
        "--json", action="store_true", default=None, help=_MACHINE_READABLE_OUTPUT_HELP
    )
    return parser


def _add_settings_argument(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--settings",
        type=Path,
        help=(f"Versioned non-secret settings file; otherwise discover {SETTINGS_FILENAME}."),
    )


def _add_registry_authentication_arguments(
    command: argparse.ArgumentParser, *, operation: str
) -> None:
    """Add one explicit, ephemeral source of registry credentials."""

    authentication = command.add_mutually_exclusive_group()
    authentication.add_argument(
        "--password-stdin",
        action="store_true",
        help=(
            f"Read the registry password from standard input; required for {operation} "
            "unless --registry-auth-file is supplied."
        ),
    )
    authentication.add_argument(
        "--registry-auth-file",
        type=Path,
        help="Docker- or Podman-compatible auth.json with an inline Basic auth entry; overrides registry.authFile.",
    )


def _add_publication_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--registry-origin", help="Registry origin, including scheme; overrides registry.origin."
    )
    command.add_argument(
        "--repository",
        help="Metadata OCI repository path; overrides registry.artifactRepository.",
    )
    command.add_argument("--channel", help="Destination OCI tag.")
    command.add_argument("--username", help="Registry username; overrides registry.username.")
    _add_registry_authentication_arguments(command, operation="publication")
    command.add_argument(
        "--allow-insecure-loopback-http",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Allow HTTP only for localhost or 127.0.0.1 developer registries; overrides registry.allowInsecureLoopbackHttp.",
    )


def _add_catalog_publication_arguments(command: argparse.ArgumentParser) -> None:
    """Add the catalog controls shared by ordinary publish commands.

    A release normally becomes discoverable immediately.  The explicit opt-out
    preserves staged-release and catalog-administration workflows without
    making them the default authoring experience.
    """

    command.add_argument(
        "--update-catalog",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Create or advance the configured catalog index after publication (default: true).",
    )
    command.add_argument(
        "--catalog-source-id", help="Catalog source ID; overrides catalog.sourceId."
    )
    command.add_argument(
        "--catalog-repository",
        help="Catalog OCI repository path; overrides registry.catalogRepository.",
    )
    command.add_argument(
        "--catalog-channel", help="Catalog OCI tag; overrides catalog.channel."
    )
    command.add_argument(
        "--expires-in-hours",
        type=int,
        help="Catalog validity period; overrides catalog.expiresInHours.",
    )
    command.add_argument(
        "--expected-index-digest",
        help="Reject publication unless the current catalog manifest has this digest.",
    )


def _resolve_arguments(arguments: argparse.Namespace, settings: PluginBuildSettings) -> None:
    """Apply the documented command-line-over-settings precedence once."""

    if hasattr(arguments, "verbose"):
        arguments.verbose = _boolean_option(
            arguments.verbose, settings.output.verbose, default=False
        )
    if hasattr(arguments, "json"):
        arguments.json = _boolean_option(arguments.json, settings.output.json, default=False)

    if arguments.command in {"validate", "inspect", "build"}:
        _resolve_compile_arguments(arguments, settings)
    if arguments.command == "publish":
        arguments.artifact = _required_path(
            arguments.artifact or settings.publish.artifact, "artifact"
        )

    if arguments.command in {"build", "publish"}:
        _resolve_publication_arguments(arguments, settings)
        _resolve_catalog_publication_arguments(arguments, settings)
    if arguments.command == "catalog" and arguments.catalog_command in {"update", "recover"}:
        _resolve_catalog_arguments(arguments, settings)
    if arguments.command == "build":
        arguments.output = _required_path(
            arguments.output or settings.build.artifact_output,
            "metadata artifact output",
        )
        arguments.workload_settings = _resolve_workload_settings(
            arguments,
            settings.build,
            registry=settings.registry,
        )


def _resolve_compile_arguments(
    arguments: argparse.Namespace, settings: PluginBuildSettings
) -> None:
    arguments.manifest = _required_path(
        arguments.manifest or settings.manifest,
        "plugin manifest",
    )
    explicit_bindings = _parse_image_bindings(arguments.image_binding or ())
    arguments.image_bindings = {**settings.build.image_bindings, **explicit_bindings}


def _resolve_publication_arguments(
    arguments: argparse.Namespace, settings: PluginBuildSettings
) -> None:
    publish = settings.publish
    registry = settings.registry
    arguments.registry_origin = arguments.registry_origin or registry.origin
    arguments.artifact_repository = registry.artifact_repository
    arguments.repository = arguments.repository or registry.artifact_repository
    arguments.channel = arguments.channel or publish.channel
    arguments.username = arguments.username or registry.username
    if arguments.password_stdin:
        arguments.registry_auth_file = None
    else:
        arguments.registry_auth_file = arguments.registry_auth_file or registry.auth_file
    arguments.allow_insecure_loopback_http = _boolean_option(
        arguments.allow_insecure_loopback_http,
        registry.allow_insecure_loopback_http,
        default=False,
    )


def _resolve_catalog_publication_arguments(
    arguments: argparse.Namespace, settings: PluginBuildSettings
) -> None:
    """Resolve catalog release controls for a build or publish invocation."""

    catalog = settings.catalog
    arguments.update_catalog = _boolean_option(
        arguments.update_catalog, settings.publish.update_catalog, default=True
    )
    if not arguments.update_catalog:
        return
    arguments.catalog_repository = arguments.catalog_repository or settings.registry.catalog_repository
    arguments.catalog_channel = arguments.catalog_channel or catalog.channel
    arguments.catalog_source_id = arguments.catalog_source_id or catalog.source_id
    arguments.expires_in_hours = arguments.expires_in_hours or catalog.expires_in_hours
    if arguments.expires_in_hours is not None and arguments.expires_in_hours <= 0:
        raise SettingsError("--expires-in-hours must be a positive integer")


def _resolve_catalog_arguments(
    arguments: argparse.Namespace, settings: PluginBuildSettings
) -> None:
    catalog = settings.catalog
    registry = settings.registry
    arguments.artifact = _required_path(
        arguments.artifact or settings.publish.artifact or settings.build.artifact_output,
        "published plugin artifact",
    )
    arguments.registry_origin = arguments.registry_origin or registry.origin
    arguments.artifact_repository = registry.artifact_repository
    arguments.catalog_repository = arguments.catalog_repository or registry.catalog_repository
    arguments.catalog_channel = arguments.catalog_channel or catalog.channel
    arguments.catalog_source_id = arguments.catalog_source_id or catalog.source_id
    arguments.expires_in_hours = arguments.expires_in_hours or catalog.expires_in_hours
    arguments.username = arguments.username or registry.username
    if arguments.password_stdin:
        arguments.registry_auth_file = None
    else:
        arguments.registry_auth_file = arguments.registry_auth_file or registry.auth_file
    arguments.allow_insecure_loopback_http = _boolean_option(
        arguments.allow_insecure_loopback_http,
        registry.allow_insecure_loopback_http,
        default=False,
    )
    if arguments.expires_in_hours is not None and arguments.expires_in_hours <= 0:
        raise SettingsError("--expires-in-hours must be a positive integer")


def _resolve_workload_settings(
    arguments: argparse.Namespace, build: BuildSettings, *, registry: RegistrySettings
) -> WorkloadBuildSettings | None:
    configured = build.workload
    if not arguments.with_workload:
        _reject_workload_overrides(arguments)
        return configured
    return WorkloadBuildSettings(
        engine=_workload_value(arguments.workload_engine, configured, "engine"),
        context=_workload_value(arguments.workload_context, configured, "context"),
        containerfile=_workload_value(
            arguments.workload_containerfile, configured, "containerfile"
        ),
        repository=arguments.workload_repository
        or _registry_workload_reference(arguments.registry_origin, registry.workload_repository),
        tag=_workload_value(arguments.workload_tag, configured, "tag"),
        platform=_workload_value(arguments.workload_platform, configured, "platform"),
        image_id=_workload_value(arguments.workload_image_id, configured, "image_id"),
    )


def _reject_workload_overrides(arguments: argparse.Namespace) -> None:
    overrides = (
        arguments.workload_engine,
        arguments.workload_context,
        arguments.workload_containerfile,
        arguments.workload_repository,
        arguments.workload_tag,
        arguments.workload_platform,
        arguments.workload_image_id,
    )
    if any(value is not None for value in overrides):
        raise SettingsError("--workload-* options require --with-workload")


def _workload_value(
    command_line: str | Path | None,
    configured: WorkloadBuildSettings | None,
    attribute: str,
) -> str | Path | None:
    if command_line is not None:
        return command_line
    return getattr(configured, attribute) if configured is not None else None


def _registry_workload_reference(origin: str | None, repository: str | None) -> str | None:
    """Turn a settings repository path into Podman's full pull/push reference."""

    if repository is None:
        return None
    if origin is None:
        raise SettingsError(
            "registry.origin is required when settings provide registry.workloadRepository"
        )
    parsed = urlparse(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise SettingsError("registry.origin must be an absolute registry origin")
    if repository.startswith("/") or "://" in repository:
        raise SettingsError("registry.workloadRepository must be a repository path, not an origin")
    return f"{parsed.netloc}/{repository}"


def _required_path(value: Path | None, label: str) -> Path:
    if value is None:
        raise SettingsError(f"{label} must be supplied on the command line or in settings")
    return value.expanduser()


def _boolean_option(command_line: bool | None, settings: bool | None, *, default: bool) -> bool:
    if command_line is not None:
        return command_line
    if settings is not None:
        return settings
    return default


def _init_tests(arguments: argparse.Namespace) -> int:
    """Generate local pytest tests for custom-runner Step targets."""
    try:
        written = generate_step_tests(
            arguments.manifest,
            tests_dir=arguments.tests_dir,
            force=arguments.force,
        )
    except (ScaffoldError, CompilationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"error: could not write tests: {error}", file=sys.stderr)
        return 1
    for path in written:
        print(f"Created {path}")
    hint = arguments.tests_dir if arguments.tests_dir is not None else Path("tests")
    print(f"Next: pytest {hint} -q")
    return 0


def _init_workspace(arguments: argparse.Namespace) -> int:
    """Create a contained source-only workspace without overwriting author files."""
    try:
        directory = _new_workspace_directory(arguments.directory)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    name = arguments.name or directory.name
    invalid = _invalid_init_argument(arguments, name)
    if invalid is not None:
        print(f"error: {invalid}", file=sys.stderr)
        return 2
    if directory.exists():
        print(f"error: refusing to overwrite existing path: {directory}", file=sys.stderr)
        return 2
    try:
        _write_workspace(directory, arguments, name)
    except OSError as error:
        print(f"error: could not create workspace: {error}", file=sys.stderr)
        return 1

    print(f"Created {directory / 'plugin.yaml'}")
    print(f"Created {directory / SETTINGS_FILENAME}")
    print(f"Next: cd {directory} && syntara-plugin validate --settings {SETTINGS_FILENAME}")
    return 0


def _new_workspace_directory(requested: Path) -> Path:
    """Normalize the user-selected new workspace while rejecting traversal syntax."""

    expanded = requested.expanduser()
    if ".." in expanded.parts:
        raise ValueError("workspace directory must not contain '..' path traversal")
    return expanded.resolve(strict=False)


def _invalid_init_argument(arguments: argparse.Namespace, name: str) -> str | None:
    for label, value, pattern in (
        ("namespace", arguments.namespace, _IDENTIFIER),
        ("name", name, _IDENTIFIER),
        ("action", arguments.action, _IDENTIFIER),
        ("version", arguments.version, _VERSION),
    ):
        if not pattern.fullmatch(value):
            return f"{label} is not valid: {value!r}"
    fields = arguments.fields
    if fields is not None:
        try:
            _validate_fields(_fields_payload(fields))
        except (OSError, SchemaError, ValueError) as error:
            return str(error)
    return None


def _fields_payload(value: str) -> dict[str, Any] | list[str]:
    loaded = _read_fields_file(value)
    if loaded is not None:
        return _normalize_fields_document(json.loads(loaded))
    if value.lstrip().startswith("{") or value.lstrip().startswith("["):
        return _normalize_fields_document(json.loads(value))
    return [field.strip() for field in value.split(",")]


def _read_fields_file(value: str) -> str | None:
    """Read a JSON fields file, or return None when ``value`` is inline data.

    A leading ``@`` is removed before path checks. ``Path("@../secret.json")``
    has no ``..`` part; the parent segment appears only after the prefix is
    stripped. The candidate is joined onto the working directory and then
    resolved to its real path, which must stay inside that directory. Absolute
    paths replace the base during the join and symlinks are resolved, so both
    are held to the same check and neither can stand in for ``..``. The file is
    then opened through the path found by listing the working directory, never
    through the path built from ``value``.
    """

    prefixed = value.startswith("@")
    candidate = value[1:] if prefixed else value
    if not prefixed and not candidate.endswith(".json"):
        return None
    if ".." in Path(candidate).parts:
        raise ValueError("fields file must not contain '..' path traversal")
    base_dir = os.path.realpath(os.getcwd())
    message = "fields file must not escape the working directory through path traversal"
    try:
        filepath = os.path.realpath(os.path.join(base_dir, candidate))
    except (OSError, RuntimeError) as error:
        raise ValueError(message) from error
    if os.path.commonpath([base_dir, filepath]) != base_dir:
        raise ValueError(message)
    if not prefixed and not os.path.isfile(filepath):
        return None
    discovered = _discover_contained_path(base_dir, os.path.relpath(filepath, base_dir))
    if discovered is None:
        raise ValueError(f"fields file does not exist: {candidate}")
    with open(discovered, encoding="utf-8") as handle:
        return handle.read()


def _discover_contained_path(base_dir: str, relative: str) -> str | None:
    """Return the entry path that matches ``relative`` under ``base_dir``.

    Each segment is matched against the names listed in its parent directory,
    starting at ``base_dir``. The returned path is built only from those listed
    entries, so nothing from the caller's string reaches the filesystem call.
    """

    current = base_dir
    for part in Path(relative).parts:
        with os.scandir(current) as entries:
            found = next((entry.path for entry in entries if entry.name == part), None)
        if found is None:
            return None
        current = found
    return current


def _normalize_fields_document(
    document: dict[str, Any] | list[str],
) -> dict[str, Any] | list[str]:
    if _is_field_map(document):
        return _schema_from_field_map(document)
    return document


def _is_field_map(document: object) -> bool:
    return (
        isinstance(document, dict)
        and bool(document)
        and all(isinstance(value, dict) for value in document.values())
        and not ("type" in document and "properties" in document)
    )


def _schema_from_field_map(document: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required = ["resource"]
    for name, specification in document.items():
        if name == "resource":
            raise ValueError("field 'resource' is already included")
        if not _FIELD_IDENTIFIER.fullmatch(name):
            raise ValueError(f"field is not valid: {name!r}")
        normalized = dict(specification)
        normalized.pop("name", None)
        normalized.pop("required", None)
        properties[name] = normalized
        if specification.get("required") is True:
            required.append(name)
    properties["resource"] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "description": "Required input field.",
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _validate_fields(document: dict[str, Any] | list[str]) -> None:
    if isinstance(document, list):
        _validate_field_names(document)
        return
    if not isinstance(document, dict):
        raise ValueError("fields JSON must be an object")
    _validate_fields_schema(document)


def _validate_field_names(fields: list[str]) -> None:
    if not all(isinstance(field, str) for field in fields):
        raise ValueError("fields JSON array entries must be strings")
    if len(fields) != len(set(fields)):
        raise ValueError("fields must be unique")
    for field in fields:
        if field == "resource":
            raise ValueError("field 'resource' is already included")
        if not _FIELD_IDENTIFIER.fullmatch(field):
            raise ValueError(f"field is not valid: {field!r}")


def _validate_fields_schema(document: dict[str, Any]) -> None:
    if document.get("type") != "object":
        raise ValueError("fields JSON schema must have type: object")
    required = document.get("required", [])
    if not isinstance(required, list) or "resource" not in required:
        raise ValueError("fields JSON schema must require resource")
    if not all(isinstance(field, str) for field in required):
        raise ValueError("fields JSON schema required entries must be strings")
    properties = document.get("properties", {})
    if not isinstance(properties, dict) or "resource" not in properties:
        raise ValueError("fields JSON schema must define resource")
    missing_required = sorted(set(required) - set(properties))
    if missing_required:
        raise ValueError(
            "fields JSON schema must define every required property: "
            + ", ".join(missing_required)
        )
    Draft202012Validator.check_schema(document)


def _write_workspace(directory: Path, arguments: argparse.Namespace, name: str) -> None:
    action_path = Path("steps") / arguments.action / "manifest.yaml"
    input_path = Path("schemas") / f"{arguments.action}.input.yaml"
    directory.mkdir(parents=True)
    (directory / action_path).parent.mkdir(parents=True)
    (directory / input_path).parent.mkdir(parents=True)
    (directory / "docs").mkdir()
    (directory / "plugin.yaml").write_text(
        _plugin_template(arguments.namespace, name, arguments.version, action_path),
        encoding="utf-8",
    )
    (directory / SETTINGS_FILENAME).write_text(_settings_template(name), encoding="utf-8")
    (directory / action_path).write_text(
        _action_template(arguments.action, input_path), encoding="utf-8"
    )
    payload = _fields_payload(arguments.fields) if arguments.fields is not None else []
    if isinstance(payload, dict):
        (directory / input_path).write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )
    else:
        (directory / input_path).write_text(_input_template(payload), encoding="utf-8")
    (directory / "docs" / "README.md").write_text(
        _readme_template(name, arguments.action), encoding="utf-8"
    )


def _compile_workspace(arguments: argparse.Namespace) -> int:
    """Compile through the public SDK API and print only safe compiler output."""
    try:
        result = compile_workspace(
            BuildRequest(
                arguments.manifest,
                image_bindings=arguments.image_bindings,
                require_image_bindings=arguments.command != "validate",
            )
        )
    except CompilationError as error:
        payload = {
            "ok": False,
            "diagnostics": [diagnostic.as_dict() for diagnostic in error.diagnostics],
        }
        if arguments.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            for diagnostic in error.diagnostics:
                location = diagnostic.location
                print(
                    f"{location.file}:{location.line}:{location.column}: {diagnostic.code}: {diagnostic.message}",
                    file=sys.stderr,
                )
                print(f"  fix: {diagnostic.remediation}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if arguments.command == "inspect":
        print(result.canonical_json().decode("utf-8"))
        return 0
    payload = {"ok": True, "digest": result.digest, "assetCount": len(result.assets)}
    if arguments.command == "validate" and result.deferred_image_bindings:
        payload["deferredImageBindings"] = list(result.deferred_image_bindings)
    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Valid: {arguments.manifest} ({result.digest}, {len(result.assets)} bundled assets)")
    return 0


def _build_artifact(arguments: argparse.Namespace) -> int:
    """Build an archive, optionally bind one Podman workload image, then publish it."""
    try:
        result, artifact, output, workload_image, workload_request = _build_archive(arguments)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (
        ArtifactArchiveError,
        ArtifactBuildError,
        CompilationError,
        ValueError,
        WorkloadBuildError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    payload = {**_artifact_payload(result.digest, artifact), "archive": str(output)}
    _add_workload_payload(payload, workload_image, workload_request)
    publication_status = _publish_built_archive(arguments, output, payload)
    if publication_status is not None:
        return publication_status
    _print_build_result(
        arguments, result.digest, artifact, output, workload_image, workload_request, payload
    )
    return 0


def _build_archive(
    arguments: argparse.Namespace,
) -> tuple[CompilationResult, PluginArtifact, Path, str | None, WorkloadBuildRequest | None]:
    image_bindings = dict(arguments.image_bindings)
    # Validate the complete authoring workspace before any remote side effect.
    # A custom runner binding is intentionally deferred until Podman returns
    # its registry digest, so preflight checks every other source contract.
    if arguments.with_workload:
        compile_workspace(
            BuildRequest(
                arguments.manifest,
                image_bindings=image_bindings,
                require_image_bindings=False,
            )
        )
    workload_image, workload_request = _build_workload(arguments, image_bindings)
    result = compile_workspace(BuildRequest(arguments.manifest, image_bindings=image_bindings))
    artifact = build_plugin_artifact(result)
    output = write_plugin_artifact_archive(artifact, arguments.output)
    return result, artifact, output, workload_image, workload_request


def _build_workload(
    arguments: argparse.Namespace, image_bindings: dict[str, str]
) -> tuple[str | None, WorkloadBuildRequest | None]:
    if not arguments.with_workload:
        return None, None
    request, image_id = _workload_request(
        arguments.workload_settings,
        allow_insecure_loopback_http=arguments.allow_insecure_loopback_http,
        auth_file=arguments.registry_auth_file,
    )
    _phase(arguments, "Building and pushing the custom runner image")
    image = build_and_push_workload(request, verbose=arguments.verbose)
    image_bindings[image_id] = image
    return image, request


def _add_workload_payload(
    payload: dict[str, object],
    workload_image: str | None,
    workload_request: WorkloadBuildRequest | None,
) -> None:
    if workload_image is None:
        return
    assert workload_request is not None
    payload["workloadImage"] = workload_image
    payload["workloadPlatform"] = workload_request.platform
    payload["workloadRepository"] = workload_request.repository


def _publish_built_archive(
    arguments: argparse.Namespace, output: Path, payload: dict[str, object]
) -> int | None:
    if not arguments.publish:
        return None
    try:
        _phase(arguments, "Publishing the metadata artifact")
        payload["publication"] = _publish_archive(arguments, output)
        if arguments.update_catalog:
            _phase(arguments, "Updating the catalog index")
            payload["catalog"] = _run_catalog_update(arguments, artifact_path=output)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (
        ArtifactArchiveError,
        CatalogIndexArtifactBuildError,
        CatalogIndexPublicationError,
        CatalogIndexUpdateError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    payload["ok"] = True
    return None


def _print_build_result(
    arguments: argparse.Namespace,
    digest: str,
    artifact: PluginArtifact,
    output: Path,
    workload_image: str | None,
    workload_request: WorkloadBuildRequest | None,
    payload: Mapping[str, object],
) -> None:
    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print(f"Built: {arguments.manifest} ({digest})")
    print(f"Archive: {output}")
    for name, blob in (
        ("config", artifact.config),
        ("pluginDescriptor", artifact.plugin_manifest),
        ("contentBundle", artifact.content_bundle),
    ):
        descriptor = blob.descriptor
        print(f"  {name}: {descriptor.digest} ({descriptor.media_type}, {descriptor.size} bytes)")
    if workload_image is not None:
        assert workload_request is not None
        print(f"Workload image: {workload_image}")
        print(f"Workload platform: {workload_request.platform}")
    if arguments.publish:
        publication = payload["publication"]
        assert isinstance(publication, Mapping)
        print(f"Published: {publication['immutableReference']}")
        print(f"Channel: {publication['repository']}:{publication['channel']}")
        catalog = payload.get("catalog")
        if isinstance(catalog, Mapping):
            print(f"Catalog index: {catalog['immutableReference']}")


def _publish_artifact(arguments: argparse.Namespace) -> int:
    """Publish one verified archive and normally advance its configured catalog."""
    try:
        _phase(arguments, "Publishing the metadata artifact")
        payload = _publish_archive(arguments, arguments.artifact)
        if arguments.update_catalog:
            _phase(arguments, "Updating the catalog index")
            payload["catalog"] = _run_catalog_update(
                arguments, artifact_path=arguments.artifact
            )
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (
        ArtifactArchiveError,
        CatalogIndexArtifactBuildError,
        CatalogIndexPublicationError,
        CatalogIndexUpdateError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Published: {payload['immutableReference']}")
        print(f"Channel: {payload['repository']}:{payload['channel']}")
    return 0


def _update_catalog(arguments: argparse.Namespace) -> int:
    """Advance one catalog channel during recovery or release administration."""

    try:
        payload = _run_catalog_update(arguments)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (
        ArtifactArchiveError,
        CatalogIndexArtifactBuildError,
        CatalogIndexPublicationError,
        CatalogIndexUpdateError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Catalog index: {payload['immutableReference']}")
        print(f"Channel: {payload['repository']}:{payload['channel']}")
        print(f"Generation: {payload['generation']}")
        entry = payload["entry"]
        assert isinstance(entry, Mapping)
        print(f"Entry: {entry['namespace']}/{entry['name']}:{entry['version']}")
        if payload["idempotent"]:
            print("No publication: the exact release is already in the catalog.")
    return 0


def _recover_catalog(arguments: argparse.Namespace) -> int:
    """Restore a catalog channel by continuing an accepted immutable chain."""

    try:
        payload = _run_catalog_recovery(arguments)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (
        ArtifactArchiveError,
        CatalogIndexArtifactBuildError,
        CatalogIndexPublicationError,
        CatalogIndexUpdateError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Recovered catalog index: {payload['immutableReference']}")
        print(f"Channel: {payload['repository']}:{payload['channel']}")
        print(f"Generation: {payload['generation']}")
        print(f"Previous accepted index: {payload['previousIndexDigest']}")
    return 0


def _run_catalog_update(
    arguments: argparse.Namespace, *, artifact_path: Path | None = None
) -> dict[str, object]:
    credentials = _publication_credentials(arguments, operation="catalog update")
    _require_catalog_configuration(arguments)
    artifact = read_plugin_artifact_archive(artifact_path or arguments.artifact)
    entry = _catalog_entry(artifact, _required_catalog_artifact_repository(arguments))
    publisher = _catalog_publisher(arguments, credentials)
    _phase(arguments, "Reading the current catalog index")
    current = publisher.read_current()
    _verify_expected_catalog_digest(arguments, current)
    document, idempotent = _next_catalog_document(arguments, current, entry)
    metadata = _mapping(document["metadata"], "catalog metadata")
    return _catalog_update_payload(
        arguments, publisher, current, entry, metadata, document, idempotent
    )


def _run_catalog_recovery(arguments: argparse.Namespace) -> dict[str, object]:
    if _SHA256_DIGEST.fullmatch(arguments.previous_index_digest) is None:
        raise SettingsError("--previous-index-digest must be a sha256 digest")
    credentials = _publication_credentials(arguments, operation="catalog recovery")
    _require_catalog_configuration(arguments)
    artifact = read_plugin_artifact_archive(arguments.artifact)
    entry = _catalog_entry(artifact, _required_catalog_artifact_repository(arguments))
    publisher = _catalog_publisher(arguments, credentials)
    _phase(arguments, "Reading Syntara's last accepted immutable catalog index")
    previous = publisher.read_immutable(arguments.previous_index_digest)
    now = datetime.now(UTC)
    document = recover_catalog_entry(
        previous=previous.document,
        previous_manifest_digest=previous.manifest_digest,
        entry=entry,
        source_id=arguments.catalog_source_id,
        issued_at=now,
        expires_at=now + timedelta(hours=arguments.expires_in_hours),
    )
    metadata = _mapping(document["metadata"], "catalog metadata")
    index_artifact = build_catalog_index_artifact(document)
    _phase(arguments, "Publishing the recovered catalog index")
    publication = publisher.publish(index_artifact)
    return {
        "catalogIndexDigest": publication.manifest_digest,
        "channel": publication.channel,
        "entry": entry,
        "generation": metadata["generation"],
        "immutableReference": publication.immutable_reference,
        "ok": True,
        "previousIndexDigest": previous.manifest_digest,
        "recovered": True,
        "repository": publication.repository,
    }


def _require_catalog_configuration(arguments: argparse.Namespace) -> None:
    missing = [
        label
        for label, value in (
            ("registry origin", arguments.registry_origin),
            ("catalog repository", arguments.catalog_repository),
            ("catalog channel", arguments.catalog_channel),
            ("catalog source ID", arguments.catalog_source_id),
            ("catalog expiry", arguments.expires_in_hours),
            ("username", arguments.username),
        )
        if value is None or value == ""
    ]
    if missing:
        raise SettingsError("catalog update requires: " + ", ".join(missing))


def _catalog_publisher(
    arguments: argparse.Namespace, credentials: RegistryCredentials
) -> CatalogIndexPublisher:
    return CatalogIndexPublisher(
        CatalogIndexPublicationTarget(
            registry_origin=arguments.registry_origin,
            repository=arguments.catalog_repository,
            channel=arguments.catalog_channel,
            allow_insecure_loopback_http=arguments.allow_insecure_loopback_http,
        ),
        credentials=credentials,
    )


def _verify_expected_catalog_digest(arguments: argparse.Namespace, current: object) -> None:
    if arguments.expected_index_digest is None:
        return
    actual_digest = current.manifest_digest if current is not None else None
    if actual_digest != arguments.expected_index_digest:
        raise SettingsError("current catalog digest does not match --expected-index-digest")


def _next_catalog_document(
    arguments: argparse.Namespace, current: object, entry: Mapping[str, str]
) -> tuple[dict[str, Any], bool]:
    now = datetime.now(UTC)
    return merge_catalog_entry(
        current=current.document if current is not None else None,
        current_manifest_digest=current.manifest_digest if current is not None else None,
        entry=entry,
        source_id=arguments.catalog_source_id,
        issued_at=now,
        expires_at=now + timedelta(hours=arguments.expires_in_hours),
        renew=getattr(arguments, "renew", False),
    )


def _catalog_update_payload(
    arguments: argparse.Namespace,
    publisher: CatalogIndexPublisher,
    current: object,
    entry: Mapping[str, str],
    metadata: Mapping[str, Any],
    document: Mapping[str, Any],
    idempotent: bool,
) -> dict[str, object]:
    if idempotent:
        assert current is not None
        return {
            "catalogIndexDigest": current.manifest_digest,
            "channel": arguments.catalog_channel,
            "entry": dict(entry),
            "generation": metadata["generation"],
            "idempotent": True,
            "immutableReference": f"{arguments.catalog_repository}@{current.manifest_digest}",
            "ok": True,
            "repository": arguments.catalog_repository,
        }
    index_artifact = build_catalog_index_artifact(document)
    _phase(arguments, "Publishing the catalog index")
    publication = publisher.publish(index_artifact)
    return {
        "catalogIndexDigest": publication.manifest_digest,
        "channel": publication.channel,
        "entry": dict(entry),
        "generation": metadata["generation"],
        "idempotent": False,
        "immutableReference": publication.immutable_reference,
        "ok": True,
        "previousIndexDigest": current.manifest_digest if current is not None else None,
        "repository": publication.repository,
    }


def _catalog_entry(artifact: PluginArtifact, artifact_repository: str) -> dict[str, object]:
    """Derive one catalog entry only from the verified signed plugin descriptor."""

    try:
        descriptor = json.loads(artifact.plugin_manifest.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("plugin artifact descriptor is not valid UTF-8 JSON") from error
    descriptor_mapping = _mapping(descriptor, "plugin artifact descriptor")
    metadata = _mapping(descriptor_mapping.get("metadata"), "plugin artifact metadata")
    specification = _mapping(descriptor_mapping.get("spec"), "plugin artifact spec")
    documentation = _mapping(specification.get("documentation"), "plugin artifact documentation")
    namespace = _required_string(metadata.get("namespace"), "plugin artifact metadata.namespace")
    name = _required_string(metadata.get("name"), "plugin artifact metadata.name")
    version = _required_string(metadata.get("version"), "plugin artifact metadata.version")
    documentation_digest = _required_string(
        documentation.get("digest"), "plugin artifact documentation.digest"
    )
    targets = specification.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("plugin artifact spec.targets must be a non-empty array")
    ownerships: set[str] = set()
    for index, target in enumerate(targets):
        target_mapping = _mapping(target, f"plugin artifact target {index}")
        if target_mapping.get("kind") not in {"Action", "Control", "Trigger"}:
            continue
        runtime = target_mapping.get("runtime")
        if not isinstance(runtime, Mapping):
            continue
        runtime = _mapping(runtime, f"plugin artifact target {index} runtime")
        kind = _required_string(runtime.get("kind"), f"plugin artifact target {index} runtime.kind")
        if kind not in {"platform-runner", "custom-runner"}:
            raise ValueError(f"unsupported plugin runtime kind for catalog: {kind!r}")
        ownerships.add(kind)
    return {
        "namespace": namespace,
        "name": name,
        "version": version,
        "artifactRepository": artifact_repository,
        "artifactDigest": artifact.digest,
        "documentationDigest": documentation_digest,
        "runtimeOwnerships": sorted(ownerships),
    }


def _required_catalog_artifact_repository(arguments: argparse.Namespace) -> str:
    repository = getattr(arguments, "artifact_repository", None)
    if not repository:
        raise SettingsError(
            "catalog update requires registry.artifactRepository in settings; "
            "it is the repository that holds the published plugin artifact"
        )
    return repository


def _publication_credentials(
    arguments: argparse.Namespace, *, operation: str
) -> RegistryCredentials:
    """Read registry credentials only for this publication invocation."""

    if arguments.registry_auth_file is not None:
        credentials = _credentials_from_auth_file(
            arguments.registry_auth_file, arguments.registry_origin
        )
        arguments.username = credentials.username
        return credentials
    if not arguments.password_stdin:
        raise SettingsError(f"{operation} requires --password-stdin or --registry-auth-file")
    password = sys.stdin.read().rstrip("\r\n")
    if not password:
        raise SettingsError("--password-stdin received no password")
    return RegistryCredentials(arguments.username, password)


def _credentials_from_auth_file(path: Path, registry_origin: str | None) -> RegistryCredentials:
    """Load one same-registry Basic credential from Docker/Podman auth JSON."""

    if registry_origin is None:
        raise SettingsError("registry origin is required with --registry-auth-file")
    try:
        document = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SettingsError(f"could not read registry auth file {path}: {error}") from error
    if not isinstance(document, Mapping) or not isinstance(document.get("auths"), Mapping):
        raise SettingsError("registry auth file must contain an auths object")

    parsed = urlparse(registry_origin)
    host = parsed.netloc
    if not host:
        raise SettingsError("registry origin must include a host for --registry-auth-file")
    auths = document["auths"]
    candidates = (host, registry_origin.rstrip("/"), f"https://{host}")
    record = next(
        (
            auths[candidate]
            for candidate in candidates
            if isinstance(auths.get(candidate), Mapping)
        ),
        None,
    )
    if not isinstance(record, Mapping) or not isinstance(record.get("auth"), str):
        raise SettingsError(f"registry auth file has no Basic credential for {host}")
    try:
        decoded = base64.b64decode(record["auth"], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (UnicodeDecodeError, ValueError, binascii.Error) as error:
        raise SettingsError(f"registry auth file has an invalid Basic credential for {host}") from error
    if not username or not password:
        raise SettingsError(f"registry auth file has an empty Basic credential for {host}")
    return RegistryCredentials(username, password)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _workload_request(
    settings: WorkloadBuildSettings | None,
    *,
    allow_insecure_loopback_http: bool,
    auth_file: Path | None,
) -> tuple[WorkloadBuildRequest, str]:
    """Turn selected non-secret CLI settings into one complete Podman request."""

    if settings is None:
        raise SettingsError("--with-workload requires workload settings")
    missing = [
        label
        for label, value in (
            ("engine", settings.engine),
            ("context", settings.context),
            ("containerfile", settings.containerfile),
            ("repository", settings.repository),
            ("tag", settings.tag),
            ("platform", settings.platform),
            ("imageId", settings.image_id),
        )
        if value is None
    ]
    if missing:
        raise SettingsError("--with-workload requires: " + ", ".join(missing))
    assert settings.context is not None
    assert settings.containerfile is not None
    assert settings.engine is not None
    assert settings.repository is not None
    assert settings.tag is not None
    assert settings.platform is not None
    assert settings.image_id is not None
    return (
        WorkloadBuildRequest(
            engine=settings.engine,
            context=settings.context.expanduser(),
            containerfile=settings.containerfile.expanduser(),
            repository=settings.repository,
            tag=settings.tag,
            platform=settings.platform,
            allow_insecure_loopback_http=allow_insecure_loopback_http,
            auth_file=auth_file,
        ),
        settings.image_id,
    )


def _publish_archive(arguments: argparse.Namespace, archive: Path) -> dict[str, object]:
    """Verify and publish a portable archive using already-resolved coordinates."""

    credentials = _publication_credentials(arguments, operation="publish")
    missing = [
        label
        for label, value in (
            ("registry origin", arguments.registry_origin),
            ("repository", arguments.repository),
            ("channel", arguments.channel),
            ("username", arguments.username),
        )
        if not value
    ]
    if missing:
        raise SettingsError("publish requires: " + ", ".join(missing))
    artifact = read_plugin_artifact_archive(archive)
    publication = PluginArtifactPublisher(
        PluginArtifactPublicationTarget(
            registry_origin=arguments.registry_origin,
            repository=arguments.repository,
            channel=arguments.channel,
            allow_insecure_loopback_http=arguments.allow_insecure_loopback_http,
        ),
        credentials=credentials,
    ).publish(artifact)
    return {
        **_artifact_payload(artifact.plugin_manifest.descriptor.digest, artifact),
        "archive": str(archive),
        "channel": publication.channel,
        "immutableReference": publication.immutable_reference,
        "ok": True,
        "repository": publication.repository,
    }


def _phase(arguments: argparse.Namespace, message: str) -> None:
    """Keep structured stdout clean while exposing meaningful verbose phases."""

    if getattr(arguments, "verbose", False):
        print(f"==> {message}", file=sys.stderr)


def _inspect(arguments: argparse.Namespace) -> int:
    """Inspect either source metadata or a verified portable artifact archive."""
    if arguments.manifest.name.endswith(".oci.tar"):
        if arguments.image_binding:
            print(
                "error: --image-binding applies only when inspecting a source plugin manifest",
                file=sys.stderr,
            )
            return 2
        try:
            artifact = read_plugin_artifact_archive(arguments.manifest)
        except ArtifactArchiveError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        payload = _artifact_payload(artifact.plugin_manifest.descriptor.digest, artifact)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    return _compile_workspace(arguments)


def _artifact_payload(compiled_digest: str, artifact: PluginArtifact) -> dict[str, object]:
    """Return safe, serializable OCI metadata for CLI build and publish results."""
    blobs = []
    for name, blob in (
        ("config", artifact.config),
        ("pluginDescriptor", artifact.plugin_manifest),
        ("contentBundle", artifact.content_bundle),
    ):
        descriptor = blob.descriptor
        blobs.append(
            {
                "digest": descriptor.digest,
                "mediaType": descriptor.media_type,
                "name": name,
                "size": descriptor.size,
            }
        )
    return {"artifactDigest": artifact.digest, "blobs": blobs, "compiledDigest": compiled_digest}


def _parse_image_bindings(raw_bindings: Sequence[str]) -> dict[str, str]:
    """Parse explicit ``logical-id=immutable-reference`` inputs without I/O."""
    bindings: dict[str, str] = {}
    for raw_binding in raw_bindings:
        identifier, separator, image = raw_binding.partition("=")
        if not separator or not identifier or not image:
            msg = "--image-binding must use ID=REPOSITORY@sha256:DIGEST"
            raise ValueError(msg)
        if identifier in bindings:
            msg = f"--image-binding repeats logical image ID {identifier!r}"
            raise ValueError(msg)
        bindings[identifier] = image
    return bindings


def _plugin_template(namespace: str, name: str, version: str, action_path: Path) -> str:
    """Return the minimal root manifest for the platform-runner template."""
    return (
        "apiVersion: syntara.io/v1alpha1\n"
        "kind: Plugin\n"
        "metadata:\n"
        f"  namespace: {namespace}\n"
        f"  name: {name}\n"
        f"  version: {version}\n"
        f"  displayName: {_display_name(name)}\n"
        "  description: A Syntara plugin.\n"
        "spec:\n"
        "  documentation:\n"
        "    path: docs/README.md\n"
        "  targets:\n"
        f"    - {action_path.as_posix()}\n"
    )


def _settings_template(name: str) -> str:
    """Return plugin-root settings without duplicating manifest target selection."""

    return (
        "apiVersion: syntara.io/v1alpha1\n"
        "kind: PluginBuildSettings\n"
        "manifest: plugin.yaml\n"
        "build:\n"
        f"  artifactOutput: dist/{name}.oci.tar\n"
    )


def _action_template(action: str, input_path: Path) -> str:
    """Return a compilable, non-product-specific ``http.v1`` action."""
    return (
        "apiVersion: syntara.io/v1alpha1\n"
        "kind: Action\n"
        "metadata:\n"
        f"  name: {action}\n"
        f"  displayName: {_display_name(action)}\n"
        "  description: Replace this example operation with a provider-specific action.\n"
        "spec:\n"
        "  runtime:\n"
        "    kind: platform-runner\n"
        "    driver: http.v1\n"
        "    operation:\n"
        "      method: GET\n"
        "      path: /resources/{input.resource}\n"
        "  input:\n"
        "    kind: asset\n"
        f"    path: {input_path.as_posix()}\n"
        "  output:\n"
        "    kind: inline\n"
        "    value:\n"
        "      type: object\n"
        "      description: Successful response returned by the provider.\n"
        "  error:\n"
        "    kind: inline\n"
        "    value:\n"
        "      type: object\n"
        "      description: Error response returned when the operation cannot complete.\n"
    )


def _input_template(field_names: Sequence[str]) -> str:
    """Return one bounded input schema for the generated action."""
    fields = ["resource", *field_names]
    required = "  - resource\n"
    properties = "".join(
        f"  {field}:\n"
        "    type: string\n"
        "    minLength: 1\n"
        "    maxLength: 128\n"
        f"    description: {'Required' if field == 'resource' else 'Optional'} input field.\n"
        for field in fields
    )
    return (
        "type: object\n"
        "description: Inputs accepted by this action.\n"
        "additionalProperties: false\n"
        f"required:\n{required}"
        f"properties:\n{properties}"
    )


def _readme_template(name: str, action: str) -> str:
    """Explain the generated files and their intentionally incomplete route."""
    return (
        f"# {_display_name(name)}\n\n"
        f"This workspace starts with one `{action}` platform-runner action. "
        "Replace its generic path, schemas, documentation, integration, and credential declarations "
        "before publishing it for a real provider.\n\n"
        "Validate source without network access:\n\n"
        "```sh\n"
        "syntara-plugin validate plugin.yaml\n"
        "```\n"
    )


def _display_name(value: str) -> str:
    """Turn a valid identifier into a human-readable scaffold label."""
    return " ".join(part.capitalize() for part in value.split("-"))


if __name__ == "__main__":
    raise SystemExit(main())
