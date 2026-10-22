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
import shlex
import sys
import textwrap
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

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
from .input_template import render_summary_lines, template_for
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

if TYPE_CHECKING:
    from .runner import RunExecutionError, RunReport, RunTarget, RunUsageError, TlsOptions

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MACHINE_READABLE_OUTPUT_HELP = "Print machine-readable output."
_DEFAULT_MANIFEST_NAME = "plugin.yaml"
_RUN_COMMAND_PREFIX = "syntara-plugin run "


def main(argv: Sequence[str] | None = None) -> int:
    """Run the authoring CLI and return its process status."""

    # The gRPC C core reads its verbosity when the library loads; setting it
    # here, before argument resolution can import the runner, keeps stderr
    # clean on every path.
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "run" and arguments.manifest is not None and arguments.manifest.is_dir():
        # Settings discovery follows the manifest file's directory, so resolve
        # a directory to its plugin.yaml: --manifest ws and
        # --manifest ws/plugin.yaml must behave identically.
        arguments.manifest = arguments.manifest / _DEFAULT_MANIFEST_NAME
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
        _print_settings_error(error, arguments)
        return 2
    if arguments.command == "validate":
        return _compile_workspace(arguments)
    if arguments.command == "inspect":
        return _inspect(arguments)
    if arguments.command == "run":
        return _run_command_guarded(arguments)
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


def _print_settings_error(error: SettingsError | ValueError, arguments: argparse.Namespace) -> None:
    """Print one settings or flag failure, honoring the run command's JSON contract."""

    if arguments.command == "run" and arguments.json:
        _print_error_json("SettingsError", str(error), (), ())
        return
    print(f"error: {error}", file=sys.stderr)


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

    test_init = commands.add_parser(
        "test-init",
        help="Generate local pytest tests for custom-runner Step types.",
    )
    test_init.add_argument(
        "manifest",
        type=Path,
        nargs="?",
        default=Path(_DEFAULT_MANIFEST_NAME),
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

    run = commands.add_parser(
        "run",
        help="Validate inputs and run one custom-runner Step locally.",
        description="Validate invocation inputs and run one custom-runner Step locally.",
        epilog=textwrap.dedent(
            """\
            The target is the full Step identity, the module:Class entry point,
            or a unique short name; it is inferred when the workspace declares
            exactly one runnable target. Inputs come from --input (inline JSON)
            or --input-file (a JSON file, - reads stdin). Without inputs the
            command prints a schema-derived template instead of executing and
            exits 2: the template is schema-shaped, not business-valid.

            source mode (the default) compiles the workspace and executes the
            Step through an in-process runtime. image mode runs the
            workspace's declared workload image through Podman or Docker and
            removes it afterwards; pass --address (and --tls-* for mutual
            TLS) to use a runtime you started yourself instead.

            example:
              syntara-plugin run create-issue --input '{"title": "Broken build"}'
              syntara-plugin run create-issue > input.json
            """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Step identity, module:Class entry point, or unique short name.",
    )
    run.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to plugin.yaml or its directory (default: ./plugin.yaml).",
    )
    _add_settings_argument(run)
    run_inputs = run.add_mutually_exclusive_group()
    run_inputs.add_argument(
        "--input",
        help="Inline JSON object with the invocation inputs.",
    )
    run_inputs.add_argument(
        "--input-file",
        type=Path,
        help="JSON file with the invocation inputs; - reads stdin.",
    )
    run.add_argument(
        "--mode",
        choices=("source", "image"),
        default="source",
        help="source executes from the workspace; image runs the workload image through "
        "Podman or Docker (default: source).",
    )
    run.add_argument(
        "--address",
        metavar="HOST:PORT",
        default=None,
        help="Use a runtime you started yourself at this address instead of running the "
        "workload image.",
    )
    run.add_argument(
        "--tls-ca",
        type=Path,
        help="CA bundle verifying the runtime server; --mode image only.",
    )
    run.add_argument(
        "--tls-cert",
        type=Path,
        help="Client certificate for mutual TLS; --mode image only.",
    )
    run.add_argument(
        "--tls-key",
        type=Path,
        help="Client private key for mutual TLS; --mode image only.",
    )
    run.add_argument("--json", action="store_true", default=None, help=_MACHINE_READABLE_OUTPUT_HELP)
    run.add_argument(
        "--verbose",
        action="store_true",
        default=None,
        help="Show execution phases and step progress on stderr.",
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
    if arguments.command == "run":
        _resolve_run_arguments(arguments, settings)
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


def _resolve_run_arguments(
    arguments: argparse.Namespace, settings: PluginBuildSettings
) -> None:
    """Resolve the run command's small option set with shared precedence."""

    from .runner import is_loopback_address, is_runtime_address

    arguments.manifest = arguments.manifest or settings.manifest or Path(_DEFAULT_MANIFEST_NAME)
    transport_options = (arguments.address, arguments.tls_ca, arguments.tls_cert, arguments.tls_key)
    if arguments.mode == "source" and any(value is not None for value in transport_options):
        raise SettingsError("--address and the --tls-* options require --mode image")
    if arguments.address is not None and not is_runtime_address(arguments.address):
        raise SettingsError(
            "--address must be a HOST:PORT TCP address, for example 127.0.0.1:50051"
        )
    tls_files = transport_options[1:]
    if (
        arguments.mode == "image"
        and arguments.address is not None
        and not is_loopback_address(arguments.address)
        and not any(value is not None for value in tls_files)
    ):
        raise SettingsError(
            "a non-loopback --address requires mutual TLS "
            "(--tls-ca, --tls-cert, --tls-key); the runtime server refuses "
            "cleartext listeners off loopback"
        )
    if any(value is not None for value in tls_files) and not all(
        value is not None for value in tls_files
    ):
        raise SettingsError("--tls-ca, --tls-cert, and --tls-key must be supplied together")


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
    return None


def _write_workspace(directory: Path, arguments: argparse.Namespace, name: str) -> None:
    action_path = Path("steps") / arguments.action / "manifest.yaml"
    input_path = Path("schemas") / f"{arguments.action}.input.yaml"
    directory.mkdir(parents=True)
    (directory / action_path).parent.mkdir(parents=True)
    (directory / input_path).parent.mkdir(parents=True)
    (directory / "docs").mkdir()
    (directory / _DEFAULT_MANIFEST_NAME).write_text(
        _plugin_template(arguments.namespace, name, arguments.version, action_path),
        encoding="utf-8",
    )
    (directory / SETTINGS_FILENAME).write_text(_settings_template(name), encoding="utf-8")
    (directory / action_path).write_text(
        _action_template(arguments.action, input_path), encoding="utf-8"
    )
    (directory / input_path).write_text(_input_template(), encoding="utf-8")
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
        _print_compilation_failure(arguments, error)
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


def _print_compilation_failure(
    arguments: argparse.Namespace,
    error: CompilationError,
    *,
    next_steps: Sequence[str] = (),
) -> None:
    """Print compiler diagnostics in the format shared by validate and run."""

    payload: dict[str, object] = {
        "ok": False,
        "diagnostics": [diagnostic.as_dict() for diagnostic in error.diagnostics],
    }
    if next_steps:
        payload["next"] = list(next_steps)
    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    for diagnostic in error.diagnostics:
        location = diagnostic.location
        print(
            f"{location.file}:{location.line}:{location.column}: {diagnostic.code}: {diagnostic.message}",
            file=sys.stderr,
        )
        print(f"  fix: {diagnostic.remediation}", file=sys.stderr)
    if next_steps:
        print(file=sys.stderr)
        print("Next:", file=sys.stderr)
        for step in next_steps:
            print(f"  {step}", file=sys.stderr)


def _run_command(arguments: argparse.Namespace) -> int:
    """Validate inputs and run one custom-runner Step locally."""
    # The execution engine pulls gRPC; importing it here keeps every command
    # that never executes a Step free of that import cost. The template
    # helpers are grpc-free and stay at module scope.
    from .runner import (
        RunExecutionError,
        RunUsageError,
        load_input,
        resolve_manifest,
        resolve_run_target,
        run_source,
        validate_inputs,
    )

    try:
        manifest = resolve_manifest(arguments.manifest)
    except RunUsageError as error:
        _print_run_error(_localized_error(error, arguments), arguments)
        return 2
    try:
        compiled = compile_workspace(BuildRequest(manifest, require_image_bindings=False))
    except CompilationError as error:
        _print_compilation_failure(arguments, error, next_steps=("syntara-plugin validate",))
        return 1
    except ValueError as error:
        _print_value_error(error, arguments)
        return 2

    try:
        target = resolve_run_target(compiled, arguments.target)
        if arguments.input is None and arguments.input_file is None:
            _print_run_template(arguments, target)
            return 2
        inputs, input_source = load_input(arguments.input, arguments.input_file)
        errors = validate_inputs(target.input_schema, inputs)
        if errors:
            _print_input_errors(target, input_source, errors, arguments)
            return 2
        progress = _progress_printer(arguments)
        if arguments.mode == "source":
            report = run_source(
                manifest, target, inputs, input_source, on_progress=progress
            )
        else:
            report = _run_image_mode(arguments, target, inputs, input_source, progress)
    except RunUsageError as error:
        _print_run_error(_localized_error(error, arguments), arguments)
        return 2
    except RunExecutionError as error:
        _print_run_error(_localized_error(error, arguments), arguments)
        return 1
    _print_run_report(arguments, report)
    return 0 if report.ok else 1


def _run_command_guarded(arguments: argparse.Namespace) -> int:
    """Run one command with Ctrl-C mapped to a quiet conventional exit."""

    try:
        return _run_command(arguments)
    except KeyboardInterrupt:
        return 130


def _print_value_error(error: ValueError, arguments: argparse.Namespace) -> None:
    """Print one compile-path value failure, honoring the JSON contract."""

    if arguments.json:
        _print_error_json("ValueError", str(error), (), ())
        return
    print(f"error: {error}", file=sys.stderr)


def _run_image_mode(
    arguments: argparse.Namespace,
    target: Any,
    inputs: Mapping[str, Any],
    input_source: str,
    progress: Callable[[Any], None] | None,
) -> Any:
    """Execute in image mode: managed container by default, or a given runtime.

    Without ``--address`` or TLS options the CLI runs the workspace's
    declared workload image itself (Podman or Docker) and removes it
    afterwards; passing either option means the author manages the runtime.
    """

    from .container_engine import managed_runtime
    from .runner import DEFAULT_ADDRESS, RunExecutionError, run_image

    tls = _run_tls_options(arguments)
    if arguments.address is not None or tls is not None:
        return run_image(
            arguments.address or DEFAULT_ADDRESS,
            target,
            inputs,
            input_source,
            tls=tls,
            on_progress=progress,
            # A cold runtime server needs seconds for Python and gRPC imports.
            startup_seconds=10.0,
        )
    if target.workload_image is None:
        raise RunExecutionError(
            "this workspace declares no workload image to run",
            next_steps=(
                "add spec.workload.image to plugin.yaml",
                "or start the runtime yourself and pass --address HOST:PORT",
            ),
        )
    if arguments.verbose and not arguments.json:
        print(f"==> starting workload container {target.workload_image}", file=sys.stderr)
    with managed_runtime(target.workload_image) as runtime:
        return run_image(
            runtime.address,
            target,
            inputs,
            input_source,
            tls=runtime.tls,
            on_progress=progress,
            image=runtime.image,
            startup_seconds=30.0,
            liveness=runtime.liveness,
        )


def _localized_error(
    error: RunExecutionError | RunUsageError, arguments: argparse.Namespace
) -> RunExecutionError | RunUsageError:
    """Rewrite the runner's generic next steps into commands that run here.

    ``validate`` and ``inspect`` take the manifest as a positional argument
    and fail without one in workspaces that have no settings file, and the
    runner's run suggestions cannot know which manifest or mode the author
    used; the CLI can, so it localizes every suggested command.
    """

    from .runner import RunExecutionError, RunUsageError

    manifest = getattr(arguments, "manifest", None)
    manifest_suffix = f" {manifest}" if manifest is not None else ""
    replacements = {
        "syntara-plugin validate": f"syntara-plugin validate{manifest_suffix}",
        "syntara-plugin inspect": f"syntara-plugin inspect{manifest_suffix}",
    }
    run_prefix = _run_base_options(arguments)
    localized = tuple(
        replacements.get(step, step)
        if not step.startswith(_RUN_COMMAND_PREFIX)
        else _RUN_COMMAND_PREFIX + step.removeprefix(_RUN_COMMAND_PREFIX) + run_prefix
        for step in error.next_steps
    )
    return RunExecutionError(
        error.message, failure_code=error.failure_code, detail=error.detail, next_steps=localized
    ) if isinstance(error, RunExecutionError) else RunUsageError(
        error.message, detail=error.detail, next_steps=localized
    )


def _run_option_arguments(arguments: argparse.Namespace) -> list[str]:
    """Collect every option that keeps a suggested run command on target.

    Manifest, settings, mode, address, and TLS material are all preserved so
    the suggestion executes or connects to the same thing the author invoked.
    """

    parts: list[str] = []
    if arguments.manifest is not None and str(arguments.manifest) != _DEFAULT_MANIFEST_NAME:
        parts.extend(("--manifest", str(arguments.manifest)))
    if arguments.settings is not None:
        parts.extend(("--settings", str(arguments.settings)))
    if arguments.mode == "image":
        parts.extend(("--mode", "image"))
    if arguments.address is not None:
        parts.extend(("--address", arguments.address))
    if arguments.tls_ca is not None:
        parts.extend(("--tls-ca", str(arguments.tls_ca)))
        parts.extend(("--tls-cert", str(arguments.tls_cert)))
        parts.extend(("--tls-key", str(arguments.tls_key)))
    return parts


def _run_base_options(arguments: argparse.Namespace) -> str:
    """Render the option suffix for a suggested run command, shell-quoted."""

    parts = _run_option_arguments(arguments)
    return " " + shlex.join(parts) if parts else ""


def _print_run_error(
    error: RunExecutionError | RunUsageError, arguments: argparse.Namespace | None = None
) -> None:
    """Print one run failure with its guidance and next command.

    With ``--json`` the failure becomes one machine-readable error document
    on stdout, so a consumer can parse every exit path the same way.
    """

    if arguments is not None and arguments.json:
        _print_error_json(type(error).__name__, error.message, error.detail, error.next_steps)
        return
    print(f"error: {_safe_text(error.message)}", file=sys.stderr)
    for line in error.detail:
        print(_safe_text(line), file=sys.stderr)
    if error.next_steps:
        print(file=sys.stderr)
        print("Next:", file=sys.stderr)
        for step in error.next_steps:
            print(f"  {step}", file=sys.stderr)


def _print_error_json(
    kind: str, message: str, detail: Sequence[str], next_steps: Sequence[str]
) -> None:
    """Print one failure as the machine-readable counterpart of the report."""

    payload = {
        "ok": False,
        "error": {
            "kind": kind,
            "message": message,
            "detail": list(detail),
            "nextSteps": list(next_steps),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _print_run_template(arguments: argparse.Namespace, target: RunTarget) -> None:
    """Print the schema-derived template and guidance without executing."""

    template = template_for(target.input_schema)
    print(json.dumps(template, ensure_ascii=False, indent=2))
    for line in render_summary_lines(target.identity, target.kind, target.input_schema):
        print(_safe_text(line), file=sys.stderr)
    print(file=sys.stderr)
    print(
        "The template is schema-shaped, not business-valid. Fill in real values.",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print("Nothing was executed. Next:", file=sys.stderr)
    print(f"  {_run_command_line(arguments, target)} --input-file input.json", file=sys.stderr)


def _print_input_errors(
    target: RunTarget,
    input_source: str,
    errors: Sequence[str],
    arguments: argparse.Namespace | None = None,
) -> None:
    """Print every input validation error with the fix-forward guidance."""

    if arguments is not None and arguments.json:
        _print_error_json(
            "InvalidInput",
            f"input does not satisfy the declared schema for {target.identity}",
            errors,
            (f"fix {input_source} and rerun", "or run without --input to print a template"),
        )
        return
    print(
        f"error: input does not satisfy the declared schema for {target.identity}",
        file=sys.stderr,
    )
    for line in errors:
        print(f"  {_safe_text(line)}", file=sys.stderr)
    print(file=sys.stderr)
    print("Nothing was executed. Next:", file=sys.stderr)
    print(f"  fix {input_source} and rerun", file=sys.stderr)
    print("  or run without --input/--input-file to print a template", file=sys.stderr)


def _print_run_report(arguments: argparse.Namespace, report: RunReport) -> None:
    """Print one completed invocation in human or machine-readable form."""

    if arguments.json:
        _print_run_report_json(report)
        return

    fields = _report_fields(report)
    width = max(len(label) for label, _ in fields) + 2
    for label, value in fields:
        print(f"{_run_label(label, width)}{_safe_text(value)}")

    if report.output is not None:
        print()
        print(_bold("Output:"))
        rendered = json.dumps(report.output, ensure_ascii=False, indent=2)
        print(_CONTROL_CHARACTERS.sub("", rendered))
    elif report.object_reference is not None:
        print()
        print(_bold("Output reference:") + f" {_safe_text(report.object_reference)}")

    if report.error_detail is not None:
        print()
        print(_bold("Error detail:"))
        for line in report.error_detail.splitlines():
            print(f"  {_safe_text(line)}")
    elif _needs_uncaptured_hint(report):
        print()
        print(
            _bold("Error detail:")
            + " not captured; a remote runtime discards exception details. "
            "Rerun with --mode source to see the local traceback."
        )


def _print_run_report_json(report: RunReport) -> None:
    """Print one completed invocation as a single pretty-printed JSON document."""

    payload: dict[str, object] = {
        "ok": report.ok,
        "mode": report.mode,
        "target": report.target,
        "image": report.image,
        "inputSource": report.input_source,
        "input": report.input_values,
        "invocationId": report.invocation_id,
        "workflowRunId": report.workflow_run_id,
        "status": report.status,
        "statusMessage": report.status_message,
        "output": report.output,
        "objectReference": report.object_reference,
        "outcome": report.outcome,
        "errorDetail": report.error_detail,
    }
    payload["error"] = (
        None
        if report.error is None
        else {
            "code": report.error.code,
            "message": report.error.message,
            "retryable": report.error.retryable,
        }
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _report_fields(report: RunReport) -> list[tuple[str, str]]:
    """Build the aligned label-and-value rows of one human report."""

    fields: list[tuple[str, str]] = [
        ("Target", report.target),
        ("Mode", report.mode),
    ]
    if report.image is not None:
        fields.append(("Image", report.image))
    fields.extend(
        [
            ("Input", json.dumps(report.input_values, ensure_ascii=False)),
            ("Invocation", report.invocation_id),
        ]
    )
    if report.status is not None:
        fields.append(("Status", str(report.status)))
        if report.status_message:
            fields.append(("Status message", report.status_message))
    if report.error is not None:
        retryable = str(report.error.retryable).lower()
        fields.append(("Error", f"{report.error.code} (retryable: {retryable})"))
        if report.error.message:
            fields.append(("Message", report.error.message))
    if report.outcome is not None:
        fields.append(("Outcome", report.outcome))
    return fields


def _needs_uncaptured_hint(report: RunReport) -> bool:
    """Return whether the image-mode failure should mention its missing detail."""

    return (
        report.error is not None
        and report.error.code == "OUTCOME_UNKNOWN"
        and report.mode == "image"
    )


def _run_label(label: str, width: int) -> str:
    """Render one aligned report label, bolded on an interactive terminal."""

    return _bold(f"{label + ':':<{width}}")


def _bold(text: str) -> str:
    """Bold on an interactive terminal; plain when piped or NO_COLOR is set."""

    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[1m{text}\033[0m"


# Free-form text from manifests, step failures, and tracebacks is rendered to
# the terminal; none of it may carry ANSI/OSC escape sequences, forge
# additional report lines through embedded newlines, or spoof text with
# bidirectional/zero-width characters. Tab is kept; newlines are flattened.
_CONTROL_CHARACTERS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f"
    r"\u200b-\u200f\u202a-\u202e\u2066-\u2069\u2028\u2029\ufeff]"
)


def _safe_text(text: str) -> str:
    """Make author- or runtime-supplied text safe for one terminal line."""

    flattened = _CONTROL_CHARACTERS.sub("", text).replace("\n", " ").replace("\r", " ")
    return flattened


def _run_command_line(arguments: argparse.Namespace, target: RunTarget) -> str:
    """Rebuild the invoked run command so guidance is copy-pasteable.

    Options that change what the next run executes or prints are preserved so
    an author in image mode is never guided into a source-mode rerun or away
    from the runtime they addressed. Shell-quoted so paths with spaces paste
    correctly.
    """

    parts = ["syntara-plugin", "run", *_run_option_arguments(arguments)]
    parts.append(arguments.target or target.metadata_name)
    if arguments.json:
        parts.append("--json")
    return shlex.join(parts)  # NOSONAR - printed guidance only; shlex.join quotes every component


def _run_tls_options(arguments: argparse.Namespace) -> TlsOptions | None:
    from .runner import TlsOptions

    files = (arguments.tls_ca, arguments.tls_cert, arguments.tls_key)
    if all(value is None for value in files):
        return None
    assert arguments.tls_ca is not None
    assert arguments.tls_cert is not None
    assert arguments.tls_key is not None
    return TlsOptions(ca=arguments.tls_ca, certificate=arguments.tls_cert, key=arguments.tls_key)


def _progress_printer(arguments: argparse.Namespace) -> Callable[[Any], None] | None:
    """Return a stderr progress printer for verbose human runs, else None."""

    if arguments.json or not arguments.verbose:
        return None

    def print_progress(event: Any) -> None:
        percent = f" {event.percent:.0f}%" if event.percent is not None else ""
        message = f" {_safe_text(event.message)}" if event.message else ""
        print(f"==> {event.state}{percent}{message}", file=sys.stderr)

    return print_progress


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


def _input_template() -> str:
    """Return one bounded input schema for the generated action."""
    return (
        "type: object\n"
        "description: Inputs accepted by this action.\n"
        "additionalProperties: false\n"
        "required:\n"
        "  - resource\n"
        "properties:\n"
        "  resource:\n"
        "    type: string\n"
        "    minLength: 1\n"
        "    maxLength: 128\n"
        "    description: Provider resource identifier.\n"
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
