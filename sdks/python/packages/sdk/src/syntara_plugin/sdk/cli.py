"""Developer CLI for authoring and releasing Syntara plugins.

Build creates local OCI archives for the plugin, its runtime bindings, and
workload images. Publish transports those exact archives and never rebuilds
them. Runtime bindings and workload images can be updated independently of the
plugin artifact.
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
import tempfile
import textwrap
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import yaml
from jsonschema import Draft202012Validator, SchemaError
from syntara_plugin.contracts import (
    load_bundle,
    validate_runtime_binding,
)
from syntara_plugin.runtime.yaml_loader import UniqueKeyLoader

from .artifact import ArtifactBuildError, PluginArtifact, build_plugin_artifact
from .artifact_archive import (
    ArtifactArchiveError,
    read_plugin_artifact_archive,
    write_plugin_artifact_archive,
)
from .authoring import BuildRequest, CompilationError, CompilationResult, compile_workspace
from .catalog import (
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
    RuntimeBindingArtifactPublisher,
    RuntimeBindingIndexPublisher,
)
from .runtime_binding import (
    RuntimeBindingArtifact,
    build_runtime_binding_artifact,
    resolve_runtime_binding,
)
from .runtime_binding_archive import read_runtime_binding_archive, write_runtime_binding_archive
from .runtime_binding_index import (
    build_runtime_binding_index_artifact,
    next_runtime_binding_index,
)
from .settings import (
    SETTINGS_FILENAME,
    PluginBuildSettings,
    SettingsError,
    load_settings,
)
from .signing import CosignError, sign_oci_reference, verify_oci_reference
from .testgen import ScaffoldError, generate_step_tests
from .workload import (
    WorkloadBuildError,
    WorkloadBuildRequest,
    build_workload_archive,
    inspect_workload_archive,
    publish_workload_archive,
    workload_archive_digest,
)

if TYPE_CHECKING:
    from .runner import RunExecutionError, RunReport, RunTarget, RunUsageError, TlsOptions

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_FIELD_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DIGEST_REFERENCE = re.compile(
    r"^(?:[a-z0-9.-]+(?::[0-9]+)?/)?"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"@sha256:[0-9a-f]{64}$"
)
_MACHINE_READABLE_OUTPUT_HELP = "Print machine-readable output."
_DEFAULT_MANIFEST_NAME = "plugin.yaml"
_RUN_COMMAND_PREFIX = "syntara-plugin run "
_API_VERSION = "syntara.io/v1alpha1"
_RUNTIME_BINDING_SPEC_LABEL = "RuntimeBinding spec"
_RUNTIME_BINDING_WORKLOADS_LABEL = "RuntimeBinding workloads"
_RUNTIME_BINDING_PLUGIN_LABEL = "RuntimeBinding plugin"
_RUNTIME_BINDING_SPEC_PLUGIN_LABEL = "RuntimeBinding spec.plugin"
_RELEASE_PLUGIN_LABEL = "release plugin"
_PLUGIN_ARCHIVE_LABEL = "plugin archive"
_PLUGIN_DESCRIPTOR_LABEL = "plugin descriptor"
_RUNTIME_BINDINGS_FILENAME = "runtime-bindings.yaml"
_EXPECTED_BINDING_INDEX_DIGEST_ERROR = (
    "current runtime-binding index digest does not match the expected digest"
)


@dataclass(frozen=True)
class _PluginReleaseBuild:
    artifact: PluginArtifact
    descriptor: Mapping[str, Any]
    binding_source: Mapping[str, Any]
    output: Path | None
    repository: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class _RuntimeBindingReleasePaths:
    dist_directory: Path
    release_path: Path
    resolved_path: Path


@dataclass
class _PreparedReleasePublication:
    inventory_path: Path
    credentials: RegistryCredentials
    inventory: Mapping[str, Any]
    plugin_record: Mapping[str, Any]
    binding_record: Mapping[str, Any]
    plugin_repository: str
    plugin_digest: str
    binding_repository: str
    binding_digest: str
    plugin_archive: Path
    binding_artifact: RuntimeBindingArtifact
    plugin_artifact: PluginArtifact
    binding_document: Mapping[str, Any]
    verified_images: list[tuple[Mapping[str, Any], Path]]
    index_publisher: RuntimeBindingIndexPublisher
    current_index: Any
    receipt_path: Path
    receipt: dict[str, Any]


@dataclass(frozen=True)
class _RuntimeBindingPublishState:
    artifact: RuntimeBindingArtifact
    document: Mapping[str, Any]
    plugin_repository: str
    plugin_digest: str
    binding_publisher: RuntimeBindingArtifactPublisher
    index_publisher: RuntimeBindingIndexPublisher
    current_index: Any


def main(argv: Sequence[str] | None = None) -> int:
    """Run the authoring CLI and return its process status."""

    # The gRPC C core reads its verbosity when the library loads; setting it
    # here, before argument resolution can import the runner, keeps stderr
    # clean on every path.
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
    parser = _parser()
    arguments = parser.parse_args(argv)
    _normalize_manifest_directory(arguments)
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
    except ValueError as error:
        _print_settings_error(error, arguments)
        return 2
    return _dispatch_command(arguments, parser)


def _normalize_manifest_directory(arguments: argparse.Namespace) -> None:
    if (
        arguments.command == "run"
        and arguments.manifest is not None
        and arguments.manifest.is_dir()
    ):
        # Settings discovery follows the manifest file's directory, so resolve
        # a directory to its plugin.yaml: --manifest ws and
        # --manifest ws/plugin.yaml must behave identically.
        arguments.manifest = arguments.manifest / _DEFAULT_MANIFEST_NAME


def _dispatch_command(arguments: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if arguments.command == "validate":
        return _compile_workspace(arguments)
    if arguments.command == "inspect":
        return _inspect(arguments)
    if arguments.command == "run":
        return _run_command_guarded(arguments)
    if arguments.command == "build":
        if arguments.runtime_bindings is not None:
            arguments.runtime_bindings = arguments.runtime_bindings.expanduser()
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

    init = commands.add_parser("init", help="Create one safe, compilable Action plugin workspace.")
    init.add_argument("directory", type=Path, help="New empty directory for the plugin workspace.")
    init.add_argument("--namespace", default="example", help="Plugin namespace (default: example).")
    init.add_argument("--name", help="Plugin name (default: final directory name).")
    init.add_argument("--version", default="0.1.0", help="Plugin SemVer version (default: 0.1.0).")
    init.add_argument("--action", default="request", help="Initial action name (default: request).")
    init.add_argument(
        "--runner",
        choices=("platform-runner", "custom-runner"),
        default="platform-runner",
        help="Action runner (default: platform-runner).",
    )
    init.add_argument(
        "--template",
        choices=("platform", "custom-workload"),
        default=None,
        help="Select the complete workspace template; custom-workload includes a RuntimeBinding manifest.",
    )
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
        help="Generate local pytest tests for custom-runner step types.",
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
        help="Validate inputs and run one custom-runner step type locally.",
        description="Validate invocation inputs and run one custom-runner step type locally.",
        epilog=textwrap.dedent(
            """\
            The target is the full step type identity, the module:Class entry point,
            or a unique short name; it is inferred when the workspace declares
            exactly one runnable target. Inputs come from --input (inline JSON)
            or --input-file (a JSON file, - reads stdin). Without inputs the
            command prints a schema-derived template instead of executing and
            exits 2: the template is schema-shaped, not business-valid.

            source mode (the default) compiles the workspace and executes the
            Step type through an in-process runtime. image mode runs the
            selected workload image through Podman or Docker and removes it
            afterwards. For a RuntimeBinding release, select an option with
            --image-option and its local inventory with --release. Pass
            --address (and --tls-* for mutual TLS) to use a runtime you
            started yourself instead.

            --credential NAME=SOURCE supplies a credential the Step's
            integration type declares, reading it from standard input (-), a
            file outside the workspace (file:PATH), or the environment
            (env:VAR). It is delivered as a read-only file at the approved
            secret mount, exactly as production delivers it, and never as an
            environment variable inside the Step.

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
        help="Step type identity, module:Class entry point, or unique short name.",
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
        "--credential",
        metavar="NAME=SOURCE",
        action="append",
        default=None,
        help="Supply one credential the Step's integration type declares; repeatable. "
        "SOURCE is - (standard input), file:PATH, or env:VAR. An inline value is refused.",
    )
    run.add_argument(
        "--mode",
        choices=("source", "image"),
        default="source",
        help="source executes from the workspace; image runs the workload image through "
        "Podman or Docker (default: source).",
    )
    run.add_argument(
        "--image-option",
        help="Stable option name from one workload in a built RuntimeBinding release; image mode only.",
    )
    run.add_argument(
        "--release",
        type=Path,
        help="Local dist/release.json inventory used with --image-option; image mode only.",
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
    run.add_argument(
        "--json", action="store_true", default=None, help=_MACHINE_READABLE_OUTPUT_HELP
    )
    run.add_argument(
        "--verbose",
        action="store_true",
        default=None,
        help="Show execution phases and step type progress on stderr.",
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
        if command == "validate":
            compile_command.add_argument(
                "--runtime-bindings",
                type=Path,
                help="Validate the source RuntimeBinding manifest against the selected plugin manifest.",
            )
        compile_command.add_argument(
            "--json", action="store_true", default=None, help=_MACHINE_READABLE_OUTPUT_HELP
        )
        if command == "inspect":
            compile_command.add_argument(
                "--verify",
                action="store_true",
                help="Verify a remote immutable OCI reference with Cosign; does not inspect or execute it.",
            )
            compile_command.add_argument(
                "--cosign-key",
                type=Path,
                help="Cosign public key used by --verify; signing uses this path as a private key on publish.",
            )
        if command == "build":
            compile_command.add_argument(
                "--output", type=Path, help="New OCI-layout .oci.tar output file."
            )
            compile_command.add_argument(
                "--runtime-bindings",
                type=Path,
                help="Source RuntimeBinding manifest to resolve and package; configured local image targets are built to OCI archives.",
            )
            compile_command.add_argument(
                "--binding-output",
                type=Path,
                help="Output path for the generated RuntimeBinding OCI archive.",
            )
            compile_command.add_argument(
                "--plugin-artifact-archive",
                type=Path,
                help="Existing plugin OCI archive for an image-only binding update.",
            )
            compile_command.add_argument(
                "--registry-origin", help="Registry origin used to pull private workload bases."
            )
            compile_command.add_argument(
                "--workload-repository",
                help="Default repository for workload image targets without a per-target repository.",
            )
            compile_command.add_argument(
                "--registry-auth-file",
                type=Path,
                help="Docker- or Podman-compatible auth.json used only while building workload images.",
            )
            compile_command.add_argument(
                "--allow-insecure-loopback-http",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Allow HTTP only for localhost or 127.0.0.1 developer registries.",
            )
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
    publish.add_argument(
        "--artifact-kind",
        choices=("plugin", "runtime-binding", "release"),
        default="plugin",
        help="Type of local input being published (default: plugin archive).",
    )
    _add_settings_argument(publish)
    _add_publication_arguments(publish)
    _add_catalog_publication_arguments(publish)
    publish.add_argument(
        "--cosign-key",
        type=Path,
        help="Cosign private key used to sign every published release object; omit to publish unsigned.",
    )
    publish.add_argument(
        "--cosign-verify-key",
        type=Path,
        help="Cosign public key used to verify the current RuntimeBinding index before extending it.",
    )
    publish.add_argument(
        "--allow-unverified-binding-index",
        action="store_true",
        help="Development only: allow publishing from an existing RuntimeBinding index without a trusted digest or signature.",
    )
    publish.add_argument(
        "--plugin-artifact-archive",
        type=Path,
        help="Exact plugin OCI archive linked by a RuntimeBinding archive; required for runtime-binding publication.",
    )
    publish.add_argument(
        "--verbose",
        action="store_true",
        default=None,
        help="Show concise publication phase information.",
    )
    publish.add_argument(
        "--json", action="store_true", default=None, help=_MACHINE_READABLE_OUTPUT_HELP
    )
    publish.add_argument(
        "--expected-binding-index-digest",
        help="Reject a stale binding-index writer unless the current digest matches.",
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
        "--cosign-key",
        type=Path,
        help="Cosign private key used to sign the published catalog index.",
    )
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
        "--cosign-key",
        type=Path,
        help="Cosign private key used to sign the recovered catalog index.",
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
    command.add_argument("--catalog-channel", help="Catalog OCI tag; overrides catalog.channel.")
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

    _resolve_common_arguments(arguments, settings)
    _resolve_build_settings(arguments, settings)
    _resolve_command_registry_settings(arguments, settings)
    _resolve_command_paths(arguments, settings)


def _resolve_common_arguments(arguments: argparse.Namespace, settings: PluginBuildSettings) -> None:
    if hasattr(arguments, "verbose"):
        arguments.verbose = _boolean_option(
            arguments.verbose, settings.output.verbose, default=False
        )
    if hasattr(arguments, "json"):
        arguments.json = _boolean_option(arguments.json, settings.output.json, default=False)
    for name in ("cosign_key", "cosign_verify_key"):
        value = getattr(arguments, name, None)
        if value is not None:
            setattr(arguments, name, value.expanduser())
    if arguments.command == "inspect":
        _validate_inspect_signature_arguments(arguments)


def _validate_inspect_signature_arguments(arguments: argparse.Namespace) -> None:
    if arguments.verify and arguments.cosign_key is None:
        raise SettingsError("inspect --verify requires --cosign-key")
    if arguments.cosign_key is not None and not arguments.verify:
        raise SettingsError("inspect --cosign-key requires --verify")


def _resolve_build_settings(arguments: argparse.Namespace, settings: PluginBuildSettings) -> None:
    if arguments.command in {"build", "validate"}:
        arguments.runtime_bindings = (
            arguments.runtime_bindings or settings.build.runtime_binding_manifest
        )
        if arguments.runtime_bindings is not None:
            arguments.runtime_bindings = arguments.runtime_bindings.expanduser()

    if arguments.command == "build":
        arguments.binding_output = arguments.binding_output or settings.build.runtime_binding_output
        arguments.workload_targets = settings.build.workloads
    if arguments.command in {"validate", "inspect", "build"}:
        _resolve_compile_arguments(arguments, settings)
    if arguments.command == "run":
        _resolve_run_arguments(arguments, settings)


def _resolve_command_registry_settings(
    arguments: argparse.Namespace, settings: PluginBuildSettings
) -> None:
    if arguments.command == "publish":
        arguments.artifact = _required_path(
            arguments.artifact
            or settings.publish.artifact
            or (Path("dist/release.json") if arguments.artifact_kind == "release" else None),
            "artifact",
        )
    if arguments.command == "publish":
        _resolve_publication_arguments(arguments, settings)
        _resolve_catalog_publication_arguments(arguments, settings)
    if arguments.command == "catalog" and arguments.catalog_command in {"update", "recover"}:
        _resolve_catalog_arguments(arguments, settings)


def _resolve_command_paths(arguments: argparse.Namespace, settings: PluginBuildSettings) -> None:
    if arguments.command == "publish" and arguments.plugin_artifact_archive is not None:
        arguments.plugin_artifact_archive = arguments.plugin_artifact_archive.expanduser()
    if arguments.command == "build":
        _resolve_build_arguments(arguments, settings)
        if arguments.plugin_artifact_archive is None:
            arguments.output = _required_path(
                arguments.output or settings.build.artifact_output,
                "metadata artifact output",
            )
        elif arguments.output is not None:
            arguments.output = arguments.output.expanduser()
        for name in ("runtime_bindings", "binding_output", "plugin_artifact_archive"):
            value = getattr(arguments, name, None)
            if value is not None:
                setattr(arguments, name, value.expanduser())


def _resolve_build_arguments(arguments: argparse.Namespace, settings: PluginBuildSettings) -> None:
    """Resolve registry coordinates needed to build local workload image archives."""
    registry = settings.registry
    arguments.registry_origin = arguments.registry_origin or registry.origin
    arguments.artifact_repository = registry.artifact_repository
    arguments.workload_repository = arguments.workload_repository or registry.workload_repository
    arguments.registry_auth_file = arguments.registry_auth_file or registry.auth_file
    if arguments.registry_auth_file is not None:
        arguments.registry_auth_file = arguments.registry_auth_file.expanduser()
    arguments.allow_insecure_loopback_http = _boolean_option(
        arguments.allow_insecure_loopback_http,
        registry.allow_insecure_loopback_http,
        default=False,
    )


def _resolve_compile_arguments(
    arguments: argparse.Namespace, settings: PluginBuildSettings
) -> None:
    manifest = arguments.manifest or settings.manifest
    if arguments.command == "build" and arguments.plugin_artifact_archive is not None:
        arguments.manifest = manifest.expanduser() if manifest is not None else None
    else:
        arguments.manifest = _required_path(manifest, "plugin manifest")
    explicit_bindings = _parse_image_bindings(arguments.image_binding or ())
    arguments.image_bindings = {**settings.build.image_bindings, **explicit_bindings}


def _resolve_run_arguments(arguments: argparse.Namespace, settings: PluginBuildSettings) -> None:
    """Resolve the run command's small option set with shared precedence."""

    arguments.manifest = arguments.manifest or settings.manifest or Path(_DEFAULT_MANIFEST_NAME)
    transport_options = (arguments.address, arguments.tls_ca, arguments.tls_cert, arguments.tls_key)
    if arguments.release is not None:
        arguments.release = arguments.release.expanduser()
    _validate_run_mode_options(arguments, transport_options)
    _validate_run_transport_options(arguments, transport_options)


def _validate_run_mode_options(
    arguments: argparse.Namespace, transport_options: Sequence[object]
) -> None:
    if arguments.mode == "source" and any(value is not None for value in transport_options):
        raise SettingsError("--address and the --tls-* options require --mode image")
    if arguments.mode == "source" and (
        arguments.image_option is not None or arguments.release is not None
    ):
        raise SettingsError("--image-option and --release require --mode image")
    if (arguments.image_option is None) != (arguments.release is None):
        raise SettingsError("--image-option and --release must be supplied together")
    if arguments.image_option is not None and any(value is not None for value in transport_options):
        raise SettingsError(
            "--image-option and --release cannot be used with a separately managed runtime"
        )


def _validate_run_transport_options(
    arguments: argparse.Namespace, transport_options: Sequence[object]
) -> None:
    from .runner import is_loopback_address, is_runtime_address

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
    if arguments.credential and any(value is not None for value in transport_options):
        # Only a container this command started can be given a mount; a
        # runtime the author manages must be given its own credential files.
        raise SettingsError(
            "--credential cannot be used with --address or the --tls-* options: "
            "the CLI does not own that runtime and cannot mount a credential into it"
        )


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
    """Resolve catalog release controls for a publish invocation."""

    catalog = settings.catalog
    arguments.update_catalog = _boolean_option(
        arguments.update_catalog, settings.publish.update_catalog, default=True
    )
    if not arguments.update_catalog:
        return
    arguments.catalog_repository = (
        arguments.catalog_repository or settings.registry.catalog_repository
    )
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


def _registry_workload_reference(origin: str | None, repository: str | None) -> str | None:
    """Turn a settings repository path into Podman's full pull/push reference."""

    if repository is None:
        return None
    if origin is None:
        raise SettingsError(
            "registry.origin is required when registry.workloadRepository is configured"
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
    """Generate local pytest tests for custom-runner step type targets."""
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
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
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
        if isinstance(specification.get("required"), bool):
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
    _validate_resource_property(properties["resource"])
    missing_required = sorted(set(required) - set(properties))
    if missing_required:
        raise ValueError(
            "fields JSON schema must define every required property: " + ", ".join(missing_required)
        )
    Draft202012Validator.check_schema(document)


def _validate_resource_property(resource: object) -> None:
    """The scaffolded action interpolates resource into the HTTP request path."""
    if not isinstance(resource, dict) or resource.get("type") != "string":
        raise ValueError("fields JSON schema must define resource as a string")
    minimum = resource.get("minLength")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        raise ValueError("fields JSON schema must set resource minLength to at least 1")


def _write_workspace(directory: Path, arguments: argparse.Namespace, name: str) -> None:
    if arguments.runner == "custom-runner":
        from .custom_scaffold import write_custom_workspace

        payload = _fields_payload(arguments.fields) if arguments.fields is not None else []
        schema = payload if isinstance(payload, dict) else _input_schema(payload)
        write_custom_workspace(
            directory, arguments.namespace, name, arguments.version, arguments.action, schema
        )
        return
    action_path = Path("steps") / arguments.action / "manifest.yaml"
    input_path = Path("schemas") / f"{arguments.action}.input.yaml"
    directory.mkdir(parents=True)
    (directory / action_path).parent.mkdir(parents=True)
    custom_workload = arguments.template == "custom-workload"
    if not custom_workload:
        (directory / input_path).parent.mkdir(parents=True)
    (directory / "docs").mkdir()
    package_name = name.replace("-", "_")
    (directory / _DEFAULT_MANIFEST_NAME).write_text(
        _plugin_template(
            arguments.namespace,
            name,
            arguments.version,
            action_path,
            custom_workload=custom_workload,
        ),
        encoding="utf-8",
    )
    (directory / SETTINGS_FILENAME).write_text(
        _settings_template(
            name,
            namespace=arguments.namespace,
            version=arguments.version,
            custom_workload=custom_workload,
        ),
        encoding="utf-8",
    )
    action_manifest = _action_template(
        arguments.action,
        input_path,
        custom_workload=custom_workload,
        package_name=package_name,
    )
    (directory / action_path).write_text(action_manifest, encoding="utf-8")
    if not custom_workload:
        payload = _fields_payload(arguments.fields) if arguments.fields is not None else []
        if isinstance(payload, dict):
            (directory / input_path).write_text(
                yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
            )
        else:
            (directory / input_path).write_text(_input_template(payload), encoding="utf-8")
    (directory / "docs" / "README.md").write_text(
        _readme_template(name, arguments.action, custom_workload=custom_workload),
        encoding="utf-8",
    )
    if custom_workload:
        _write_custom_workload_scaffold(
            directory,
            name=name,
            namespace=arguments.namespace,
            version=arguments.version,
            package_name=package_name,
            action_path=action_path,
            action_manifest=action_manifest,
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

    binding_validated, failure_code = _validate_workspace_binding(arguments, result)
    if failure_code is not None:
        return failure_code
    _print_workspace_result(arguments, result, binding_validated)
    return 0


def _validate_workspace_binding(
    arguments: argparse.Namespace, result: CompilationResult
) -> tuple[bool, int | None]:
    if arguments.command != "validate" or arguments.runtime_bindings is None:
        return False, None
    try:
        binding_source = _read_runtime_binding_source(arguments.runtime_bindings)
        binding_spec = _mapping(binding_source.get("spec"), _RUNTIME_BINDING_SPEC_LABEL)
        plugin_link = _mapping(binding_spec.get("plugin"), _RUNTIME_BINDING_PLUGIN_LABEL)
        if "source" not in plugin_link:
            raise SettingsError(
                "validate --runtime-bindings accepts a source manifest; use build or inspect "
                "for a RuntimeBinding linked to an existing plugin artifact"
            )
        binding_plugin = (
            arguments.runtime_bindings.parent
            / _required_string(plugin_link.get("source"), f"{_RUNTIME_BINDING_PLUGIN_LABEL} source")
        ).resolve()
        if binding_plugin != arguments.manifest.resolve():
            raise SettingsError(
                "RuntimeBinding plugin.source must resolve to the selected plugin manifest"
            )
        diagnostics = validate_runtime_binding(
            load_bundle(), binding_source, resolved=False, plugin_descriptor=result.descriptor
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return False, 2
    if diagnostics:
        for diagnostic in diagnostics:
            print(f"{diagnostic.code}: {diagnostic.message}", file=sys.stderr)
        return False, 1
    return True, None


def _print_workspace_result(
    arguments: argparse.Namespace, result: CompilationResult, binding_validated: bool
) -> None:
    if arguments.command == "inspect":
        print(result.canonical_json().decode("utf-8"))
        return
    payload = {"ok": True, "digest": result.digest, "assetCount": len(result.assets)}
    if binding_validated:
        payload["runtimeBinding"] = {"valid": True, "path": str(arguments.runtime_bindings)}
    if arguments.command == "validate" and result.deferred_image_bindings:
        payload["deferredImageBindings"] = list(result.deferred_image_bindings)
    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Valid: {arguments.manifest} ({result.digest}, {len(result.assets)} bundled assets)")


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
    """Validate inputs and run one custom-runner step type locally."""
    # The execution engine pulls gRPC; importing it here keeps every command
    # that never executes a step type free of that import cost. The template
    # helpers are grpc-free and stay at module scope.
    from . import runner

    try:
        manifest = runner.resolve_manifest(arguments.manifest)
    except runner.RunUsageError as error:
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
        invocation = _prepare_run_invocation(arguments, manifest, compiled, runner)
        if invocation is None:
            return 2
        target, inputs, input_source, credentials, warnings = invocation
        _print_run_warnings(warnings)
        report = _execute_run_invocation(
            arguments, manifest, target, inputs, input_source, credentials, runner
        )
    except runner.RunUsageError as error:
        _print_run_error(_localized_error(error, arguments), arguments)
        return 2
    except runner.RunExecutionError as error:
        _print_run_error(_localized_error(error, arguments), arguments)
        return 1
    _print_run_report(arguments, report)
    return 0 if report.ok else 1


def _prepare_run_invocation(
    arguments: argparse.Namespace,
    manifest: Path,
    compiled: CompilationResult,
    runner: Any,
) -> tuple[Any, dict[str, Any], str, Any, Sequence[str]] | None:
    target = runner.resolve_run_target(compiled, arguments.target)
    # Parse before the template check so malformed credentials remain usage errors.
    specs = runner.parse_credential_options(arguments.credential)
    if arguments.input is None and arguments.input_file is None:
        _print_run_template(arguments, target)
        return None
    if any(spec.kind == "stdin" for spec in specs) and str(arguments.input_file) == "-":
        raise runner.RunUsageError(
            "only one of --input-file - and --credential NAME=- can read standard input",
            next_steps=("supply the input in a file, or the credential with file: or env:",),
        )
    inputs, input_source = runner.load_input(arguments.input, arguments.input_file)
    errors = runner.validate_inputs(target.input_schema, inputs)
    if errors:
        _print_input_errors(target, input_source, errors, arguments)
        return None
    credentials, warnings = runner.load_credentials(
        specs,
        runner.reachable_credential_recipes(compiled, target),
        target,
        workspace=manifest.parent,
    )
    if credentials and arguments.mode == "source":
        warnings = (
            *warnings,
            "warning: source mode delivers credential files at a temporary path, "
            "not /var/run/syntara/secrets as in production; a Step must use "
            "context.credential(name).read() rather than a mount path",
        )
    return target, inputs, input_source, credentials, warnings


def _execute_run_invocation(
    arguments: argparse.Namespace,
    manifest: Path,
    target: Any,
    inputs: Mapping[str, Any],
    input_source: str,
    credentials: Any,
    runner: Any,
) -> Any:
    progress = _progress_printer(arguments)
    if arguments.mode == "source":
        return runner.run_source(
            manifest, target, inputs, input_source, on_progress=progress, credentials=credentials
        )
    if arguments.image_option is not None and arguments.release is not None:
        return _run_selected_release_image(
            arguments, target, inputs, input_source, credentials, progress
        )
    return _run_image_mode(arguments, target, inputs, input_source, progress, credentials)


def _run_selected_release_image(
    arguments: argparse.Namespace,
    target: Any,
    inputs: Mapping[str, Any],
    input_source: str,
    credentials: Any,
    progress: Callable[[Any], None],
) -> Any:
    image_reference, image_archive, image_digest = _image_option_from_release(
        arguments.release, target=target, option_name=arguments.image_option
    )
    image_target = replace(target, workload_image=image_reference)
    if image_archive is None:
        return _run_image_mode(arguments, image_target, inputs, input_source, progress, credentials)
    from .container_engine import load_oci_archive

    with load_oci_archive(image_archive, expected_digest=image_digest) as local_reference:
        report = _run_image_mode(
            arguments,
            replace(image_target, workload_image=local_reference),
            inputs,
            input_source,
            progress,
            credentials,
        )
    return replace(report, image=image_reference)


def _run_command_guarded(arguments: argparse.Namespace) -> int:
    """Run one command with Ctrl-C mapped to a quiet conventional exit."""

    try:
        return _run_command(arguments)
    except KeyboardInterrupt:
        return 130


def _image_option_from_release(
    inventory_path: Path,
    *,
    target: Any,
    option_name: str,
) -> tuple[str, Path | None, str]:
    """Resolve one image option from verified local P and RuntimeBinding archives."""
    from .runner import RunUsageError

    try:
        inventory, plugin_descriptor, binding_document = _load_release_binding_documents(
            inventory_path
        )
        image_reference, image_digest = _resolve_binding_image_option(
            binding_document, plugin_descriptor, target, option_name
        )
        image_archive = _find_release_image_archive(
            inventory_path, inventory, image_reference, image_digest
        )
        return image_reference, image_archive, image_digest
    except RunUsageError:
        raise
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise RunUsageError(
            "could not resolve the requested image option from the local release",
            detail=(str(error),),
            next_steps=(
                "inspect the release inventory and its plugin and RuntimeBinding archives",
                "run syntara-plugin build before using image mode",
            ),
        ) from error


def _load_release_binding_documents(
    inventory_path: Path,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    inventory = _read_release_inventory(inventory_path)
    plugin_record = _mapping(inventory.get("plugin"), _RELEASE_PLUGIN_LABEL)
    binding_record = _mapping(inventory.get("runtimeBinding"), "release runtimeBinding")
    plugin_digest = _required_string(plugin_record.get("digest"), "release plugin digest")
    plugin_archive = _release_inventory_path(
        inventory_path, plugin_record.get("archive"), _PLUGIN_ARCHIVE_LABEL
    )
    plugin_artifact = read_plugin_artifact_archive(plugin_archive)
    if plugin_artifact.digest != plugin_digest:
        raise ArtifactArchiveError("plugin archive digest does not match the release inventory")
    plugin_descriptor = _decode_json_object(
        plugin_artifact.plugin_manifest.content, _PLUGIN_DESCRIPTOR_LABEL
    )
    binding_archive = _release_inventory_path(
        inventory_path, binding_record.get("archive"), "runtime-binding archive"
    )
    binding_artifact = read_runtime_binding_archive(
        binding_archive,
        plugin_descriptor=plugin_descriptor,
        plugin_manifest_descriptor=plugin_artifact.manifest.descriptor,
    )
    binding_digest = _required_string(
        binding_record.get("digest"), "release runtime-binding digest"
    )
    if binding_artifact.digest != binding_digest:
        raise ArtifactArchiveError("RuntimeBinding digest does not match the release inventory")
    binding_document = _decode_json_object(
        binding_artifact.binding_document.content, "RuntimeBinding document"
    )
    plugin_link = _mapping(
        _mapping(binding_document.get("spec"), _RUNTIME_BINDING_SPEC_LABEL).get("plugin"),
        _RUNTIME_BINDING_PLUGIN_LABEL,
    )
    if plugin_link.get("digest") != plugin_digest:
        raise ArtifactArchiveError("RuntimeBinding links a different plugin artifact")
    return inventory, plugin_descriptor, binding_document


def _resolve_binding_image_option(
    binding_document: Mapping[str, Any],
    plugin_descriptor: Mapping[str, Any],
    target: Any,
    option_name: str,
) -> tuple[str, str]:
    specification = _mapping(binding_document.get("spec"), _RUNTIME_BINDING_SPEC_LABEL)
    contract = _mapping(specification.get("contract"), "RuntimeBinding contract")
    step_revision_ids = contract.get("stepRevisionIds")
    if not isinstance(step_revision_ids, list):
        raise ArtifactArchiveError("RuntimeBinding contract has no step-revision list")
    if target.workload_id is None or target.step_revision_id not in step_revision_ids:
        raise SettingsError("the selected step is not included in this RuntimeBinding release")
    _validate_target_workload(plugin_descriptor, target)
    workload_options = _binding_workload_options(specification, target.workload_id)
    option = next((item for item in workload_options if item.get("name") == option_name), None)
    if option is None:
        raise SettingsError(
            f"image option {option_name!r} is not available for workload {target.workload_id!r}"
        )
    image_reference = _required_string(option.get("image"), "RuntimeBinding image reference")
    if _DIGEST_REFERENCE.fullmatch(image_reference) is None:
        raise ArtifactArchiveError("RuntimeBinding image is not an immutable digest reference")
    return image_reference, image_reference.rsplit("@", 1)[1]


def _validate_target_workload(plugin_descriptor: Mapping[str, Any], target: Any) -> None:
    descriptor_spec = _mapping(plugin_descriptor.get("spec"), "plugin descriptor spec")
    descriptor_targets = descriptor_spec.get("targets")
    if not isinstance(descriptor_targets, list):
        raise ArtifactArchiveError("plugin descriptor has no target list")
    plugin_target = next(
        (
            item
            for item in descriptor_targets
            if isinstance(item, Mapping) and item.get("step_revision_id") == target.step_revision_id
        ),
        None,
    )
    runtime = (
        _mapping(plugin_target.get("runtime"), "plugin target runtime")
        if isinstance(plugin_target, Mapping)
        else {}
    )
    if runtime.get("kind") != "custom-runner" or runtime.get("workload") != target.workload_id:
        raise ArtifactArchiveError("RuntimeBinding workload does not match the selected step")


def _binding_workload_options(
    specification: Mapping[str, Any], workload_id: str
) -> list[Mapping[str, Any]]:
    workloads = _mapping(specification.get("workloads"), _RUNTIME_BINDING_WORKLOADS_LABEL)
    workload = _mapping(workloads.get(workload_id), "selected RuntimeBinding workload")
    options = workload.get("options")
    if not isinstance(options, list):
        raise ArtifactArchiveError("selected RuntimeBinding workload has no option list")
    return [
        _mapping(option, "RuntimeBinding image option")
        for option in options
        if isinstance(option, Mapping)
    ]


def _find_release_image_archive(
    inventory_path: Path,
    inventory: Mapping[str, Any],
    image_reference: str,
    image_digest: str,
) -> Path | None:
    images = inventory.get("images")
    if not isinstance(images, list):
        raise SettingsError("release inventory images must be a list")
    for position, raw_image in enumerate(images):
        image = _mapping(raw_image, f"release images[{position}]")
        if image.get("reference") != image_reference:
            continue
        candidate_digest = _required_string(image.get("digest"), "release image digest")
        if candidate_digest != image_digest:
            raise ArtifactArchiveError("release image digest does not match its reference")
        archive = _release_inventory_path(
            inventory_path,
            image.get("archive"),
            f"image archive {image.get('buildTarget', position)}",
        )
        if workload_archive_digest(archive) != image_digest:
            raise ArtifactArchiveError("workload archive digest does not match the RuntimeBinding")
        return archive
    return None


def _print_run_warnings(warnings: Sequence[str]) -> None:
    """Print pre-execution warnings on stderr, where the human channels live.

    Warnings stay on stderr under --json too: stdout owes that consumer
    exactly one JSON document, and a warning is not part of it.
    """

    for warning in warnings:
        print(_safe_text(warning), file=sys.stderr)


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
    credentials: Sequence[Any] = (),
) -> Any:
    """Execute in image mode: managed container by default, or a given runtime.

    Without ``--address`` or TLS options the CLI runs the workspace's
    declared workload image itself (Podman or Docker) and removes it
    afterwards; passing either option means the author manages the runtime.

    Credentials exist as files only while the container runs: they are
    written into a private directory, mounted read-only at the approved
    secret mount, and removed when the block ends. ``--credential`` is
    rejected earlier for a runtime the author manages, which this command
    cannot mount anything into.
    """

    from .container_engine import managed_runtime
    from .runner import (
        DEFAULT_ADDRESS,
        RunExecutionError,
        credential_files,
        run_image,
        run_secret_scrubber,
    )

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
    with (
        credential_files(credentials, container_readable=True) as (_, host_paths),
        managed_runtime(
            target.workload_image,
            target,
            host_paths,
            # The engine reads a dead container's logs before the runner's
            # health check exists, so it scrubs them as it reads them.
            scrub=run_secret_scrubber(target.input_schema, inputs, credentials),
        ) as runtime,
    ):
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
            credentials=credentials,
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
    return (
        RunExecutionError(
            error.message,
            failure_code=error.failure_code,
            detail=error.detail,
            next_steps=localized,
        )
        if isinstance(error, RunExecutionError)
        else RunUsageError(error.message, detail=error.detail, next_steps=localized)
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
    if arguments.image_option is not None:
        parts.extend(("--image-option", arguments.image_option))
    if arguments.release is not None:
        parts.extend(("--release", str(arguments.release)))
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
            _bold("Error detail:") + " not captured; a remote runtime discards exception details. "
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
        "credentials": list(report.credentials),
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
            "providerCode": report.error.provider_code,
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
    fields.append(("Input", json.dumps(report.input_values, ensure_ascii=False)))
    if report.credentials:
        # Names only: the report is one of the surfaces a value must not reach.
        fields.append(("Credentials", ", ".join(report.credentials)))
    fields.append(("Invocation", report.invocation_id))
    if report.status is not None:
        fields.append(("Status", str(report.status)))
        if report.status_message:
            fields.append(("Status message", report.status_message))
    if report.error is not None:
        retryable = str(report.error.retryable).lower()
        fields.append(("Error", f"{report.error.code} (retryable: {retryable})"))
        if report.error.message:
            fields.append(("Message", report.error.message))
        if report.error.provider_code:
            fields.append(("Provider code", report.error.provider_code))
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
    """Build local immutable metadata archives without publishing registry content."""
    if arguments.plugin_artifact_archive is not None and arguments.runtime_bindings is None:
        print("error: --plugin-artifact-archive requires --runtime-bindings", file=sys.stderr)
        return 2
    if arguments.binding_output is not None and arguments.runtime_bindings is None:
        print("error: --binding-output requires --runtime-bindings", file=sys.stderr)
        return 2
    if arguments.runtime_bindings is not None:
        return _build_runtime_binding_release(arguments)
    try:
        result, artifact, output = _build_archive(arguments)
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
    _print_build_result(arguments, result.digest, artifact, output, payload)
    return 0


def _build_runtime_binding_release(arguments: argparse.Namespace) -> int:
    """Build P when requested, resolve a RuntimeBinding source, and package B locally."""
    try:
        plugin_build = _prepare_plugin_release_build(arguments)
        paths, plugin_archive = _prepare_runtime_binding_release_paths(
            arguments, plugin_build.output
        )
        output, binding_output, binding_artifact, resolved_document, image_archives = (
            _create_runtime_binding_release_artifacts(
                arguments, plugin_build, paths, plugin_archive
            )
        )
        _write_runtime_binding_release_outputs(
            arguments,
            plugin_build,
            paths,
            output,
            binding_output,
            binding_artifact,
            resolved_document,
            image_archives,
        )
        payload = _runtime_binding_release_payload(
            plugin_build,
            paths,
            output,
            binding_output,
            binding_artifact,
            resolved_document,
            image_archives,
            output or arguments.plugin_artifact_archive,
        )
    except (CompilationError, ValueError, OSError, WorkloadBuildError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Plugin artifact: {plugin_build.artifact.digest}")
        if output is not None:
            print(f"Plugin archive: {output}")
        print(f"Runtime binding: {binding_artifact.digest}")
        print(f"Resolved manifest: {paths.resolved_path}")
        print(f"Runtime binding archive: {binding_output}")
        print("No registry content was published.")
    return 0


def _prepare_plugin_release_build(arguments: argparse.Namespace) -> _PluginReleaseBuild:
    if arguments.plugin_artifact_archive is not None:
        artifact = read_plugin_artifact_archive(arguments.plugin_artifact_archive)
        descriptor = _decode_json_object(artifact.plugin_manifest.content, _PLUGIN_DESCRIPTOR_LABEL)
        binding_source = _read_runtime_binding_source(arguments.runtime_bindings)
        plugin_link = _runtime_binding_plugin_link(binding_source)
        repository = _release_plugin_repository(arguments, plugin_link, artifact.digest)
        payload = _artifact_payload(artifact.plugin_manifest.descriptor.digest, artifact)
        return _PluginReleaseBuild(artifact, descriptor, binding_source, None, repository, payload)
    if arguments.manifest is None:
        raise SettingsError("a plugin manifest is required when no plugin archive is supplied")
    if arguments.output is None:
        raise SettingsError("--output is required to build the plugin artifact")
    result = compile_workspace(
        BuildRequest(
            arguments.manifest,
            image_bindings=arguments.image_bindings,
            require_image_bindings=False,
            bind_workload_images=False,
        )
    )
    artifact = build_plugin_artifact(result)
    binding_source = _read_runtime_binding_source(arguments.runtime_bindings)
    plugin_link = _runtime_binding_plugin_link(binding_source)
    source_path = plugin_link.get("source")
    if not isinstance(source_path, str):
        raise SettingsError(
            "building a new plugin release requires RuntimeBinding spec.plugin.source"
        )
    if (arguments.runtime_bindings.parent / source_path).resolve() != arguments.manifest.resolve():
        raise SettingsError(
            "RuntimeBinding plugin.source must resolve to the plugin manifest being built"
        )
    repository = arguments.artifact_repository
    if not repository:
        raise SettingsError(
            "registry.artifactRepository is required to link the RuntimeBinding to the plugin"
        )
    return _PluginReleaseBuild(
        artifact,
        dict(result.descriptor),
        binding_source,
        arguments.output,
        repository,
        _artifact_payload(result.digest, artifact),
    )


def _runtime_binding_plugin_link(binding_source: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(
        _mapping(binding_source.get("spec"), _RUNTIME_BINDING_SPEC_LABEL).get("plugin"),
        _RUNTIME_BINDING_SPEC_PLUGIN_LABEL,
    )


def _release_plugin_repository(
    arguments: argparse.Namespace, plugin_link: Mapping[str, Any], plugin_digest: str
) -> str:
    artifact_reference = plugin_link.get("artifact")
    if artifact_reference is not None:
        artifact_reference = _mapping(artifact_reference, "RuntimeBinding spec.plugin.artifact")
        repository = _required_string(
            artifact_reference.get("repository"), "RuntimeBinding plugin repository"
        )
        if artifact_reference.get("digest") != plugin_digest:
            raise SettingsError(
                "RuntimeBinding plugin digest does not match --plugin-artifact-archive"
            )
        return repository
    if isinstance(plugin_link.get("source"), str):
        repository = arguments.artifact_repository
        if repository:
            return repository
        raise SettingsError(
            "registry.artifactRepository is required when resolving a RuntimeBinding "
            "source against --plugin-artifact-archive"
        )
    raise SettingsError(
        "RuntimeBinding plugin must identify a source manifest or immutable artifact"
    )


def _prepare_runtime_binding_release_paths(
    arguments: argparse.Namespace, output: Path | None
) -> tuple[_RuntimeBindingReleasePaths, Path | None]:
    dist_directory = arguments.runtime_bindings.parent.resolve() / "dist"
    if dist_directory.is_symlink():
        raise SettingsError("refusing to write generated files through a dist symlink")
    dist_directory.mkdir(parents=True, exist_ok=True)
    if output is not None:
        output = _release_output_path(output, dist_directory, _PLUGIN_ARCHIVE_LABEL)
        arguments.output = output
    if arguments.plugin_artifact_archive is not None:
        arguments.plugin_artifact_archive = _release_output_path(
            arguments.plugin_artifact_archive, dist_directory, _PLUGIN_ARCHIVE_LABEL
        )
    if arguments.binding_output is not None:
        arguments.binding_output = _release_output_path(
            arguments.binding_output, dist_directory, "RuntimeBinding archive"
        )
    release_path = _release_output_path(
        dist_directory / "release.json", dist_directory, "release inventory"
    )
    resolved_path = _release_output_path(
        dist_directory / _RUNTIME_BINDINGS_FILENAME, dist_directory, "resolved RuntimeBinding"
    )
    if arguments.runtime_bindings.resolve() == resolved_path.resolve():
        raise SettingsError(
            f"the source RuntimeBinding cannot be dist/{_RUNTIME_BINDINGS_FILENAME} because build writes the resolved manifest there"
        )
    if release_path.exists():
        _read_release_inventory(release_path)
    known_outputs = [
        path
        for path in (
            output,
            arguments.plugin_artifact_archive,
            arguments.binding_output,
            resolved_path,
            release_path,
        )
        if path is not None
    ]
    _validate_distinct_paths(known_outputs)
    return _RuntimeBindingReleasePaths(dist_directory, release_path, resolved_path), output


def _validate_distinct_paths(paths: Sequence[Path]) -> None:
    if len({path.resolve() for path in paths}) != len(paths):
        raise SettingsError("release inventory, manifests, and archives must use different paths")


def _create_runtime_binding_release_artifacts(
    arguments: argparse.Namespace,
    plugin_build: _PluginReleaseBuild,
    paths: _RuntimeBindingReleasePaths,
    output: Path | None,
) -> tuple[Path | None, Path, RuntimeBindingArtifact, Mapping[str, Any], list[dict[str, str]]]:
    _validate_existing_plugin_output(output, plugin_build.artifact)
    images = _build_runtime_binding_images(
        plugin_build.binding_source,
        plugin_build.descriptor,
        arguments,
        dist_directory=paths.dist_directory,
    )
    image_bindings = dict(arguments.image_bindings)
    image_bindings.update({item["buildTarget"]: item["reference"] for item in images})
    resolved_document = resolve_runtime_binding(
        plugin_build.binding_source,
        plugin_build.descriptor,
        plugin_repository=plugin_build.repository,
        plugin_digest=plugin_build.artifact.digest,
        image_bindings=image_bindings,
    )
    artifact = build_runtime_binding_artifact(
        resolved_document,
        plugin_build.descriptor,
        plugin_build.artifact.manifest.descriptor,
    )
    binding_output = arguments.binding_output or (
        paths.dist_directory / f"runtime-bindings-{artifact.digest.removeprefix('sha256:')}.oci.tar"
    )
    binding_output = _release_output_path(
        binding_output, paths.dist_directory, "RuntimeBinding archive"
    )
    output_paths = [
        path
        for path in (
            output,
            arguments.plugin_artifact_archive,
            binding_output,
            paths.resolved_path,
            paths.release_path,
            *(Path(item["archive"]) for item in images),
        )
        if path is not None
    ]
    _validate_distinct_paths(output_paths)
    return output, binding_output, artifact, resolved_document, images


def _validate_existing_plugin_output(output: Path | None, artifact: PluginArtifact) -> None:
    if output is None or not output.exists():
        return
    existing = read_plugin_artifact_archive(output)
    if existing.digest != artifact.digest:
        raise ArtifactArchiveError(
            f"refusing to replace a different plugin artifact at {output}; "
            "choose a new build.artifactOutput path"
        )


def _write_runtime_binding_release_outputs(
    arguments: argparse.Namespace,
    plugin_build: _PluginReleaseBuild,
    paths: _RuntimeBindingReleasePaths,
    output: Path | None,
    binding_output: Path,
    binding_artifact: RuntimeBindingArtifact,
    resolved_document: Mapping[str, Any],
    images: Sequence[Mapping[str, str]],
) -> None:
    if output is not None and not output.exists():
        write_plugin_artifact_archive(plugin_build.artifact, output)
    _write_or_verify_binding_archive(binding_output, binding_artifact, plugin_build)
    _write_generated_yaml(paths.resolved_path, resolved_document)
    _write_release_inventory(
        paths.release_path,
        plugin_repository=plugin_build.repository,
        plugin_digest=plugin_build.artifact.digest,
        plugin_archive=output or arguments.plugin_artifact_archive,
        binding_repository=arguments.artifact_repository or plugin_build.repository,
        binding_digest=binding_artifact.digest,
        binding_archive=binding_output,
        images=images,
        publish_plugin=output is not None,
    )


def _write_or_verify_binding_archive(
    binding_output: Path,
    binding_artifact: RuntimeBindingArtifact,
    plugin_build: _PluginReleaseBuild,
) -> None:
    if binding_output.exists():
        existing = read_runtime_binding_archive(
            binding_output,
            plugin_descriptor=plugin_build.descriptor,
            plugin_manifest_descriptor=plugin_build.artifact.manifest.descriptor,
        )
        if existing.digest != binding_artifact.digest:
            raise ArtifactArchiveError(
                f"refusing to replace a different RuntimeBinding archive at {binding_output}; "
                "choose a new --binding-output path"
            )
    else:
        write_runtime_binding_archive(binding_artifact, binding_output)


def _runtime_binding_release_payload(
    plugin_build: _PluginReleaseBuild,
    paths: _RuntimeBindingReleasePaths,
    output: Path | None,
    binding_output: Path,
    binding_artifact: RuntimeBindingArtifact,
    resolved_document: Mapping[str, Any],
    images: Sequence[Mapping[str, str]],
    plugin_archive: Path | None,
) -> dict[str, Any]:
    return {
        "bindingArtifactDigest": binding_artifact.digest,
        "bindingArchive": str(binding_output),
        "bindingDocument": str(paths.resolved_path),
        "bindingDocumentDigest": binding_artifact.binding_document.descriptor.digest,
        "contractBundleDigest": load_bundle().digest,
        "ok": True,
        "plugin": plugin_build.payload,
        "pluginArtifactDigest": plugin_build.artifact.digest,
        "pluginArchive": str(output or plugin_archive),
        "releaseInventory": str(paths.release_path),
        "images": list(images),
        "workloads": resolved_document["spec"]["workloads"],
    }


def _build_runtime_binding_images(
    source: Mapping[str, Any],
    descriptor: Mapping[str, Any],
    arguments: argparse.Namespace,
    *,
    dist_directory: Path,
) -> list[dict[str, str]]:
    """Build each locally configured binding target as a registered OCI archive."""
    targets = _runtime_binding_build_targets(source)
    configured = getattr(arguments, "workload_targets", {})
    if not isinstance(configured, Mapping):
        configured = {}
    if not targets or not configured:
        return []
    image_directory = _prepare_workload_image_directory(dist_directory)
    selected = _select_workload_builds(targets, configured, arguments)
    return [
        _build_one_workload_image(
            build_target,
            workload_id,
            settings,
            repository,
            descriptor,
            arguments,
            image_directory,
        )
        for build_target, workload_id, settings, repository in selected
    ]


def _runtime_binding_build_targets(source: Mapping[str, Any]) -> dict[str, str]:
    specification = _mapping(source.get("spec"), _RUNTIME_BINDING_SPEC_LABEL)
    workloads = _mapping(specification.get("workloads"), _RUNTIME_BINDING_WORKLOADS_LABEL)
    targets: dict[str, str] = {}
    for workload_id, workload_value in workloads.items():
        workload = _mapping(workload_value, f"RuntimeBinding workload {workload_id}")
        options = workload.get("options")
        if not isinstance(options, list):
            raise SettingsError(f"RuntimeBinding workload {workload_id} options must be a list")
        for option in options:
            image = _mapping(
                _mapping(option, "RuntimeBinding workload option").get("image"),
                "RuntimeBinding option image",
            )
            build_target = image.get("buildTarget")
            if not isinstance(build_target, str):
                continue
            previous = targets.setdefault(build_target, workload_id)
            if previous != workload_id:
                raise SettingsError(
                    f"buildTarget {build_target!r} cannot serve multiple logical workloads"
                )
    return targets


def _prepare_workload_image_directory(dist_directory: Path) -> Path:
    dist_directory.mkdir(parents=True, exist_ok=True)
    if dist_directory.is_symlink():
        raise SettingsError("refusing to write generated files through a dist symlink")
    image_directory = dist_directory / "images"
    if image_directory.is_symlink():
        raise SettingsError("refusing to write generated files through an images symlink")
    image_directory.mkdir(parents=True, exist_ok=True)
    return image_directory


def _select_workload_builds(
    targets: Mapping[str, str], configured: Mapping[str, Any], arguments: argparse.Namespace
) -> list[tuple[str, str, Any, str]]:
    selected: list[tuple[str, str, Any, str]] = []
    for build_target, workload_id in sorted(targets.items()):
        settings = configured.get(build_target)
        if settings is None:
            continue
        repository = settings.repository or _registry_workload_reference(
            arguments.registry_origin, getattr(arguments, "workload_repository", None)
        )
        _validate_workload_build_settings(build_target, settings, repository)
        selected.append((build_target, workload_id, settings, repository))
    return selected


def _validate_workload_build_settings(
    build_target: str, settings: Any, repository: str | None
) -> None:
    missing = [
        name
        for name, value in (
            ("context", settings.context),
            ("containerfile", settings.containerfile),
            ("tag", settings.tag),
            ("platform", settings.platform),
            ("repository", repository),
        )
        if value is None
    ]
    if missing:
        raise SettingsError(f"build.workloads.{build_target} requires: " + ", ".join(missing))
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", build_target):
        raise SettingsError(f"buildTarget {build_target!r} is not a safe archive filename")


def _build_one_workload_image(
    build_target: str,
    workload_id: str,
    settings: Any,
    repository: str,
    descriptor: Mapping[str, Any],
    arguments: argparse.Namespace,
    image_directory: Path,
) -> dict[str, str]:
    request = WorkloadBuildRequest(
        engine=settings.engine or "podman",
        context=settings.context,
        containerfile=settings.containerfile,
        repository=repository,
        tag=settings.tag,
        platform=settings.platform,
        allow_insecure_loopback_http=arguments.allow_insecure_loopback_http,
        auth_file=arguments.registry_auth_file,
        registration_descriptor=descriptor,
        workload_id=workload_id,
    )
    with tempfile.TemporaryDirectory(prefix=f".{build_target}-", dir=image_directory) as directory:
        temporary_archive = Path(directory) / "image.oci.tar"
        reference = build_workload_archive(request, temporary_archive, verbose=arguments.verbose)
        if not temporary_archive.is_file():
            raise WorkloadBuildError(
                f"container engine did not create workload archive for {build_target!r}"
            )
        digest = workload_archive_digest(temporary_archive)
        if reference != f"{repository}@{digest}":
            raise WorkloadBuildError(
                f"container engine returned an inconsistent digest for {build_target!r}"
            )
        destination = image_directory / f"{build_target}-{digest.removeprefix('sha256:')}.oci.tar"
        _store_workload_image_archive(temporary_archive, destination, digest)
    return {
        "archive": str(destination),
        "buildTarget": build_target,
        "digest": digest,
        "platform": settings.platform,
        "reference": reference,
        "repository": repository,
        "tag": settings.tag,
        "workload": workload_id,
    }


def _store_workload_image_archive(temporary_archive: Path, destination: Path, digest: str) -> None:
    if destination.is_symlink():
        raise SettingsError(
            f"refusing to reuse a workload archive through a symlink: {destination}"
        )
    if destination.exists():
        if workload_archive_digest(destination) != digest:
            raise WorkloadBuildError(
                f"content-addressed workload archive has the wrong digest: {destination}"
            )
        return
    os.replace(temporary_archive, destination)


def _write_release_inventory(
    path: Path,
    *,
    plugin_repository: str,
    plugin_digest: str,
    plugin_archive: Path | None,
    binding_repository: str,
    binding_digest: str,
    binding_archive: Path,
    images: Sequence[Mapping[str, str]],
    publish_plugin: bool,
) -> None:
    """Write a local-only inventory that records the exact publication order."""
    root = path.parent.resolve()

    def relative_archive(archive: Path | None) -> str | None:
        if archive is None:
            return None
        resolved = _release_output_path(archive, root, "release archive")
        return resolved.relative_to(root).as_posix()

    value = {
        "apiVersion": _API_VERSION,
        "kind": "PluginReleaseInventory",
        "images": [dict(item) for item in images],
        "plugin": {
            "archive": relative_archive(plugin_archive),
            "digest": plugin_digest,
            "publish": publish_plugin,
            "repository": plugin_repository,
        },
        "runtimeBinding": {
            "archive": relative_archive(binding_archive),
            "digest": binding_digest,
            "repository": binding_repository,
        },
    }
    for image in value["images"]:
        image["archive"] = relative_archive(Path(image["archive"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _read_runtime_binding_source(path: Path) -> dict[str, Any]:
    """Read one bounded YAML source document and reject duplicate keys."""
    try:
        content = path.expanduser().read_bytes()
    except OSError as error:
        raise SettingsError(f"could not read RuntimeBinding source {path}: {error}") from error
    if not content or len(content) > 1_048_576:
        raise SettingsError("RuntimeBinding source must be between 1 byte and 1 MiB")
    try:
        value = yaml.load(content, Loader=UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise SettingsError(f"RuntimeBinding source is not valid YAML: {error}") from error
    return dict(_mapping(value, "RuntimeBinding source"))


def _decode_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactArchiveError(f"{label} is not valid UTF-8 JSON") from error
    return dict(_mapping(value, label))


def _write_generated_yaml(path: Path, document: Mapping[str, Any]) -> None:
    """Atomically replace one generated resolved manifest below dist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, yaml.safe_dump(dict(document), sort_keys=False))


def _build_archive(
    arguments: argparse.Namespace,
) -> tuple[CompilationResult, PluginArtifact, Path]:
    image_bindings = dict(arguments.image_bindings)
    result = compile_workspace(BuildRequest(arguments.manifest, image_bindings=image_bindings))
    artifact = build_plugin_artifact(result)
    output = write_plugin_artifact_archive(artifact, arguments.output)
    return result, artifact, output


def _print_build_result(
    arguments: argparse.Namespace,
    digest: str,
    artifact: PluginArtifact,
    output: Path,
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


def _publish_artifact(arguments: argparse.Namespace) -> int:
    """Publish one verified archive and normally advance its configured catalog."""
    if arguments.artifact_kind == "release":
        return _publish_release_command(arguments)
    if arguments.artifact_kind == "runtime-binding":
        return _publish_runtime_binding_command(arguments)
    return _publish_metadata_artifact(arguments)


def _publish_release_command(arguments: argparse.Namespace) -> int:
    try:
        payload = _publish_release(arguments, arguments.artifact)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (CatalogIndexPublicationError, ValueError, WorkloadBuildError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Published release inventory: {payload['releaseInventory']}")
        for publication in payload["publications"]:
            print(f"  {publication['kind']}: {publication['immutableReference']}")
        if "catalog" in payload:
            print(f"Catalog index: {payload['catalog']['immutableReference']}")
    return 0


def _publish_runtime_binding_command(arguments: argparse.Namespace) -> int:
    try:
        payload = _publish_runtime_binding_release(arguments, arguments.artifact)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (ValueError, OSError, CatalogIndexPublicationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Published runtime binding: {payload['immutableReference']}")
        print(f"Channel: {payload['repository']}:{payload['channel']}")
    return 0


def _publish_metadata_artifact(arguments: argparse.Namespace) -> int:
    try:
        _phase(arguments, "Publishing the metadata artifact")
        payload = _publish_archive(arguments, arguments.artifact)
        payload["signature"] = (
            "signed"
            if _sign_reference(arguments, str(payload["immutableReference"]))
            else "unsigned"
        )
        if arguments.update_catalog:
            _phase(arguments, "Updating the catalog index")
            payload["catalog"] = _run_catalog_update(arguments, artifact_path=arguments.artifact)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (CatalogIndexPublicationError, ValueError, WorkloadBuildError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"Published: {payload['immutableReference']}")
        print(f"Channel: {payload['repository']}:{payload['channel']}")
    return 0


def _publish_release(arguments: argparse.Namespace, inventory_path: Path) -> dict[str, Any]:
    """Publish one verified local release inventory in dependency order."""
    prepared = _prepare_release_publication(arguments, inventory_path)
    _write_release_publication_receipt(prepared.receipt_path, prepared.receipt)
    _publish_release_images(arguments, prepared)
    _publish_release_plugin(arguments, prepared)
    _publish_release_binding(arguments, prepared)
    index_reference, index_digest, idempotent = _publish_release_binding_index(arguments, prepared)
    signature = _sign_reference(
        arguments,
        _cosign_immutable_reference(arguments, prepared.plugin_repository, index_digest),
    )
    prepared.receipt["publications"].append(
        {
            "kind": "runtime-binding-index",
            "immutableReference": index_reference,
            "idempotent": idempotent,
            "signature": "signed" if signature else "unsigned",
        }
    )
    _write_release_publication_receipt(prepared.receipt_path, prepared.receipt)
    result: dict[str, Any] = {
        "bindingIndexDigest": index_digest,
        "bindingIndexReference": index_reference,
        "ok": True,
        "publications": prepared.receipt["publications"],
        "signature": "signed" if signature else "unsigned",
        "releaseInventory": str(inventory_path),
        "receipt": str(prepared.receipt_path),
    }
    _update_release_catalog(arguments, prepared, result)
    prepared.receipt["ok"] = True
    _write_release_publication_receipt(prepared.receipt_path, prepared.receipt)
    return result


def _prepare_release_publication(
    arguments: argparse.Namespace, inventory_path: Path
) -> _PreparedReleasePublication:
    credentials = _publication_credentials(arguments, operation="release publish")
    if not arguments.registry_origin or not arguments.username:
        raise SettingsError("release publish requires registry.origin and registry.username")
    inventory = _read_release_inventory(inventory_path)
    plugin_record, binding_record = _release_records(inventory)
    (
        plugin_repository,
        plugin_digest,
        binding_repository,
        binding_digest,
        plugin_archive,
        binding_archive,
    ) = _release_identity_and_paths(inventory_path, plugin_record, binding_record)
    _validate_release_publication_settings(
        arguments, plugin_record, plugin_repository, binding_repository
    )
    binding_artifact, plugin_artifact, binding_document = _read_linked_runtime_binding_archive(
        binding_archive, plugin_archive, expected_plugin_repository=plugin_repository
    )
    _verify_release_artifact_identity(
        plugin_repository,
        plugin_digest,
        binding_digest,
        plugin_artifact,
        binding_artifact,
        binding_document,
    )
    verified_images = _verify_release_images(inventory_path, inventory, binding_document)
    index_publisher, current_index = _prepare_release_binding_index(
        arguments, credentials, plugin_repository, plugin_digest
    )
    receipt_path = inventory_path.with_name("release-publication.json")
    receipt: dict[str, Any] = {
        "apiVersion": _API_VERSION,
        "kind": "PluginReleasePublication",
        "releaseInventory": str(inventory_path),
        "publications": [],
    }
    return _PreparedReleasePublication(
        inventory_path,
        credentials,
        inventory,
        plugin_record,
        binding_record,
        plugin_repository,
        plugin_digest,
        binding_repository,
        binding_digest,
        plugin_archive,
        binding_artifact,
        plugin_artifact,
        binding_document,
        verified_images,
        index_publisher,
        current_index,
        receipt_path,
        receipt,
    )


def _release_records(
    inventory: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    plugin_record = _mapping(inventory["plugin"], _RELEASE_PLUGIN_LABEL)
    binding_record = _mapping(inventory["runtimeBinding"], "release runtimeBinding")
    if set(plugin_record) != {"archive", "digest", "publish", "repository"}:
        raise SettingsError("release plugin has unknown or missing fields")
    if set(binding_record) != {"archive", "digest", "repository"}:
        raise SettingsError("release runtimeBinding has unknown or missing fields")
    if not isinstance(plugin_record.get("publish"), bool):
        raise SettingsError("release plugin publish must be a boolean")
    return plugin_record, binding_record


def _release_identity_and_paths(
    inventory_path: Path,
    plugin_record: Mapping[str, Any],
    binding_record: Mapping[str, Any],
) -> tuple[str, str, str, str, Path, Path]:
    plugin_repository = _required_string(plugin_record.get("repository"), "plugin repository")
    plugin_digest = _required_string(plugin_record.get("digest"), "plugin digest")
    binding_repository = _required_string(binding_record.get("repository"), "binding repository")
    binding_digest = _required_string(binding_record.get("digest"), "binding digest")
    plugin_archive_value = plugin_record.get("archive")
    if plugin_archive_value is None:
        raise SettingsError(
            "release publish requires the exact plugin archive to verify its RuntimeBinding"
        )
    plugin_archive = _release_inventory_path(
        inventory_path, plugin_archive_value, _PLUGIN_ARCHIVE_LABEL
    )
    binding_archive = _release_inventory_path(
        inventory_path, binding_record.get("archive"), "runtime-binding archive"
    )
    return (
        plugin_repository,
        plugin_digest,
        binding_repository,
        binding_digest,
        plugin_archive,
        binding_archive,
    )


def _validate_release_publication_settings(
    arguments: argparse.Namespace,
    plugin_record: Mapping[str, Any],
    plugin_repository: str,
    binding_repository: str,
) -> None:
    if plugin_record.get("publish") and (
        arguments.repository != plugin_repository or not arguments.channel
    ):
        raise SettingsError(
            "release plugin repository must match registry.artifactRepository and publish.channel must be set"
        )
    if binding_repository != plugin_repository:
        raise SettingsError("runtime bindings must be stored in the plugin artifact repository")
    if arguments.expires_in_hours is None or arguments.expires_in_hours <= 0:
        raise SettingsError("release publish requires catalog.expiresInHours for the binding index")
    if plugin_record.get("publish") and arguments.update_catalog:
        _require_catalog_configuration(arguments)


def _verify_release_artifact_identity(
    plugin_repository: str,
    plugin_digest: str,
    binding_digest: str,
    plugin_artifact: PluginArtifact,
    binding_artifact: RuntimeBindingArtifact,
    binding_document: Mapping[str, Any],
) -> None:
    if plugin_artifact.digest != plugin_digest:
        raise ArtifactArchiveError("plugin archive digest does not match the release inventory")
    if binding_artifact.digest != binding_digest:
        raise ArtifactArchiveError(
            "runtime-binding archive digest does not match the release inventory"
        )
    plugin_link = _runtime_binding_plugin_link(binding_document)
    if (
        plugin_link.get("repository") != plugin_repository
        or plugin_link.get("digest") != plugin_digest
    ):
        raise ArtifactArchiveError("RuntimeBinding points to a different plugin artifact")


def _release_binding_image_references(binding_document: Mapping[str, Any]) -> set[str]:
    specification = _mapping(binding_document.get("spec"), _RUNTIME_BINDING_SPEC_LABEL)
    workloads = _mapping(specification.get("workloads"), _RUNTIME_BINDING_WORKLOADS_LABEL)
    return {
        _required_string(
            _mapping(option, "RuntimeBinding image option").get("image"), "image reference"
        )
        for workload in workloads.values()
        for option in _mapping(workload, "RuntimeBinding workload")["options"]
    }


def _verify_release_images(
    inventory_path: Path, inventory: Mapping[str, Any], binding_document: Mapping[str, Any]
) -> list[tuple[Mapping[str, Any], Path]]:
    images = inventory.get("images")
    if not isinstance(images, list):
        raise SettingsError("release inventory images must be a list")
    binding_references = _release_binding_image_references(binding_document)
    verified: list[tuple[Mapping[str, Any], Path]] = []
    seen_targets: set[str] = set()
    for position, raw in enumerate(images):
        image = _verify_release_image(
            inventory_path, position, raw, seen_targets, binding_references
        )
        verified.append(image)
    return verified


def _verify_release_image(
    inventory_path: Path,
    position: int,
    raw_image: Any,
    seen_targets: set[str],
    binding_references: set[str],
) -> tuple[Mapping[str, Any], Path]:
    image = _mapping(raw_image, f"release images[{position}]")
    expected_keys = {
        "archive",
        "buildTarget",
        "digest",
        "platform",
        "reference",
        "repository",
        "tag",
        "workload",
    }
    if set(image) != expected_keys:
        raise SettingsError(f"release images[{position}] has unknown or missing fields")
    build_target = _required_string(image.get("buildTarget"), "image buildTarget")
    if build_target in seen_targets:
        raise SettingsError(f"release inventory repeats image build target {build_target!r}")
    seen_targets.add(build_target)
    archive = _release_inventory_path(
        inventory_path, image.get("archive"), f"image archive {build_target}"
    )
    digest = workload_archive_digest(archive)
    if digest != image.get("digest"):
        raise WorkloadBuildError(f"workload archive {build_target!r} changed after build")
    repository = _required_string(image.get("repository"), "image repository")
    _required_string(image.get("tag"), "image tag")
    reference = f"{repository}@{digest}"
    if image.get("reference") != reference or reference not in binding_references:
        raise ArtifactArchiveError(
            f"workload archive {build_target!r} is not linked by the RuntimeBinding"
        )
    return image, archive


def _prepare_release_binding_index(
    arguments: argparse.Namespace,
    credentials: RegistryCredentials,
    plugin_repository: str,
    plugin_digest: str,
) -> tuple[RuntimeBindingIndexPublisher, Any]:
    publisher = RuntimeBindingIndexPublisher(
        CatalogIndexPublicationTarget(
            registry_origin=arguments.registry_origin,
            repository=plugin_repository,
            channel=_runtime_binding_index_channel(plugin_digest),
            allow_insecure_loopback_http=arguments.allow_insecure_loopback_http,
        ),
        credentials=credentials,
        verify_current_index=_runtime_binding_index_verifier(arguments, plugin_repository),
        allow_unverified_current=arguments.allow_unverified_binding_index,
    )
    current = publisher.read_current()
    actual_digest = current.manifest_digest if current is not None else None
    if (
        arguments.expected_binding_index_digest is not None
        and actual_digest != arguments.expected_binding_index_digest
    ):
        raise SettingsError(_EXPECTED_BINDING_INDEX_DIGEST_ERROR)
    return publisher, current


def _record_release_publication(
    prepared: _PreparedReleasePublication,
    publication: Mapping[str, Any],
) -> None:
    prepared.receipt["publications"].append(dict(publication))
    _write_release_publication_receipt(prepared.receipt_path, prepared.receipt)


def _publish_release_images(
    arguments: argparse.Namespace, prepared: _PreparedReleasePublication
) -> None:
    with _temporary_skopeo_auth_file(arguments, prepared.credentials) as auth_file:
        for image, archive in prepared.verified_images:
            _publish_release_image(arguments, prepared, image, archive, auth_file)


def _publish_release_image(
    arguments: argparse.Namespace,
    prepared: _PreparedReleasePublication,
    image: Mapping[str, Any],
    archive: Path,
    auth_file: Path,
) -> None:
    _phase(arguments, f"Publishing workload image {image['buildTarget']}")
    published_reference = publish_workload_archive(
        archive,
        f"{image['repository']}:{image['tag']}",
        auth_file=auth_file,
        allow_insecure_loopback_http=arguments.allow_insecure_loopback_http,
        verbose=arguments.verbose,
    )
    if published_reference != image["reference"]:
        raise WorkloadBuildError(
            f"registry did not preserve workload digest for {image['buildTarget']}"
        )
    signature = _sign_reference(
        arguments,
        _cosign_immutable_reference(arguments, str(image["repository"]), str(image["digest"])),
    )
    _record_release_publication(
        prepared,
        {
            "kind": "workload-image",
            "immutableReference": published_reference,
            "signature": "signed" if signature else "unsigned",
        },
    )


def _publish_release_plugin(
    arguments: argparse.Namespace, prepared: _PreparedReleasePublication
) -> None:
    if not prepared.plugin_record.get("publish"):
        return
    _phase(arguments, "Publishing plugin metadata")
    published = _publish_archive(
        arguments, prepared.plugin_archive, credentials=prepared.credentials
    )
    expected = f"{prepared.plugin_repository}@{prepared.plugin_digest}"
    if published["immutableReference"] != expected:
        raise ArtifactArchiveError("published plugin digest differs from the prepared release")
    signature = _sign_reference(
        arguments,
        _cosign_immutable_reference(arguments, prepared.plugin_repository, prepared.plugin_digest),
    )
    _record_release_publication(
        prepared,
        {
            "kind": "plugin",
            "immutableReference": published["immutableReference"],
            "signature": "signed" if signature else "unsigned",
        },
    )


def _publish_release_binding(
    arguments: argparse.Namespace, prepared: _PreparedReleasePublication
) -> None:
    publisher = RuntimeBindingArtifactPublisher(
        PluginArtifactPublicationTarget(
            registry_origin=arguments.registry_origin,
            repository=prepared.binding_repository,
            channel=f"binding-{prepared.binding_digest.removeprefix('sha256:')}",
            allow_insecure_loopback_http=arguments.allow_insecure_loopback_http,
        ),
        credentials=prepared.credentials,
    )
    _phase(arguments, "Publishing RuntimeBinding")
    published = publisher.publish(prepared.binding_artifact)
    if published.manifest_digest != prepared.binding_digest:
        raise ArtifactArchiveError(
            "published RuntimeBinding digest differs from the prepared release"
        )
    signature = _sign_reference(
        arguments,
        _cosign_immutable_reference(
            arguments, prepared.binding_repository, prepared.binding_digest
        ),
    )
    _record_release_publication(
        prepared,
        {
            "kind": "runtime-binding",
            "immutableReference": published.immutable_reference,
            "signature": "signed" if signature else "unsigned",
        },
    )


def _publish_release_binding_index(
    arguments: argparse.Namespace, prepared: _PreparedReleasePublication
) -> tuple[str, str, bool]:
    current = prepared.current_index
    index_document, idempotent = next_runtime_binding_index(
        current=current.document if current is not None else None,
        current_manifest_digest=current.manifest_digest if current is not None else None,
        plugin_digest=prepared.plugin_digest,
        binding_repository=prepared.binding_repository,
        binding_digest=prepared.binding_digest,
        expires_in_hours=arguments.expires_in_hours,
        release=_mapping(prepared.binding_document.get("metadata"), "RuntimeBinding metadata").get(
            "release"
        ),
    )
    if idempotent:
        assert current is not None
        digest = current.manifest_digest
        reference = f"{prepared.plugin_repository}@{digest}"
    else:
        artifact = build_runtime_binding_index_artifact(index_document)
        _phase(arguments, "Publishing runtime-binding index")
        publication = prepared.index_publisher.publish(
            artifact,
            expected_current_digest=current.manifest_digest if current is not None else None,
            expected_current_etag=current.etag if current is not None else None,
        )
        reference, digest = publication.immutable_reference, publication.manifest_digest
    return reference, digest, idempotent


def _update_release_catalog(
    arguments: argparse.Namespace,
    prepared: _PreparedReleasePublication,
    result: dict[str, Any],
) -> None:
    if not (prepared.plugin_record.get("publish") and arguments.update_catalog):
        return
    _phase(arguments, "Updating plugin catalog after all release dependencies")
    result["catalog"] = _run_catalog_update(
        arguments, artifact_path=prepared.plugin_archive, credentials=prepared.credentials
    )


def _read_release_inventory(path: Path) -> Mapping[str, Any]:
    """Read a bounded, strict local release inventory document."""
    try:
        with path.expanduser().open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > 1_048_576:
                raise SettingsError("release inventory exceeds the 1 MiB safety limit")
            content = stream.read(1_048_577)
    except OSError as error:
        raise SettingsError(f"could not read release inventory: {error}") from error
    if len(content) > 1_048_576:
        raise SettingsError("release inventory exceeds the 1 MiB safety limit")
    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SettingsError(f"release inventory is not valid JSON: {error}") from error
    inventory = _mapping(value, "release inventory")
    if set(inventory) != {"apiVersion", "kind", "images", "plugin", "runtimeBinding"}:
        raise SettingsError("release inventory has unknown or missing top-level fields")
    if (
        inventory.get("apiVersion") != _API_VERSION
        or inventory.get("kind") != "PluginReleaseInventory"
    ):
        raise SettingsError("unsupported release inventory kind or apiVersion")
    return inventory


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate keys instead of silently accepting the last value."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SettingsError("release inventory contains a duplicate JSON object key")
        result[key] = value
    return result


def _release_inventory_path(inventory_path: Path, raw_path: object, label: str) -> Path:
    """Resolve an inventory archive path and keep reads inside the inventory directory."""
    if not isinstance(raw_path, str) or not raw_path:
        raise SettingsError(f"release inventory {label} path must be a non-empty string")
    root = inventory_path.expanduser().resolve().parent
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SettingsError(
            f"release inventory {label} path must be relative and stay under {root}"
        )
    candidate = root / candidate
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise SettingsError(f"release inventory {label} must not traverse symlinks")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise SettingsError(f"release inventory {label} does not exist") from error
    if os.path.commonpath((str(root), str(resolved))) != str(root):
        raise SettingsError(f"release inventory {label} must be a regular path under {root}")
    if not resolved.is_file():
        raise SettingsError(f"release inventory {label} is not a regular file")
    return resolved


def _release_output_path(path: Path, dist_directory: Path, label: str) -> Path:
    """Require release outputs and their inputs to remain portable beneath dist."""
    root = dist_directory.expanduser().resolve()
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        lexical = Path(os.path.abspath(candidate))
        relative = lexical.relative_to(root)
    except ValueError as error:
        raise SettingsError(
            f"{label} must be under {root} so the release inventory stays portable"
        ) from error
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SettingsError(f"refusing {label} path through a symlink: {current}")
    resolved = lexical.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise SettingsError(f"{label} resolves outside {root}") from error
    return resolved


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text through a unique same-directory file, then atomically replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _runtime_binding_index_channel(plugin_digest: str) -> str:
    if _SHA256_DIGEST.fullmatch(plugin_digest) is None:
        raise SettingsError("plugin artifact digest must be sha256")
    return f"runtime-bindings-{plugin_digest.removeprefix('sha256:')}"


def _write_release_publication_receipt(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically record completed release publication phases outside the inventory."""
    _atomic_write_text(path, json.dumps(value, sort_keys=True, indent=2) + "\n")


@contextmanager
def _temporary_skopeo_auth_file(
    arguments: argparse.Namespace,
    credentials: RegistryCredentials,
) -> Iterator[Path | None]:
    """Provide Skopeo one private temporary auth file without exposing credentials in argv."""
    if arguments.registry_auth_file is not None:
        yield arguments.registry_auth_file
        return
    parsed = urlparse(arguments.registry_origin)
    if not parsed.netloc:
        raise SettingsError("registry.origin must include a host for image publication")
    encoded = base64.b64encode(f"{credentials.username}:{credentials.password}".encode()).decode(
        "ascii"
    )
    with tempfile.TemporaryDirectory(prefix="syntara-registry-auth-") as directory:
        auth_path = Path(directory) / "auth.json"
        auth_path.write_text(
            json.dumps({"auths": {parsed.netloc: {"auth": encoded}}}), encoding="utf-8"
        )
        auth_path.chmod(0o600)
        yield auth_path


def _publish_runtime_binding_archive(
    arguments: argparse.Namespace,
    archive: Path,
    *,
    credentials: RegistryCredentials | None = None,
) -> dict[str, object]:
    """Publish the exact OCI bytes from one already-built RuntimeBinding archive."""
    if not arguments.registry_origin or not arguments.repository or not arguments.channel:
        raise SettingsError(
            "runtime-binding publish requires registry.origin, artifactRepository, and publish.channel"
        )
    credentials = credentials or _publication_credentials(
        arguments, operation="runtime-binding publish"
    )
    artifact = read_runtime_binding_archive(archive)
    publication = RuntimeBindingArtifactPublisher(
        PluginArtifactPublicationTarget(
            registry_origin=arguments.registry_origin,
            repository=arguments.repository,
            channel=arguments.channel,
            allow_insecure_loopback_http=arguments.allow_insecure_loopback_http,
        ),
        credentials=credentials,
    ).publish(artifact)
    return {
        "archive": str(archive),
        "bindingDocumentDigest": artifact.binding_document.descriptor.digest,
        "bindingManifestDigest": artifact.digest,
        "channel": publication.channel,
        "immutableReference": publication.immutable_reference,
        "ok": True,
        "repository": publication.repository,
    }


def _publish_runtime_binding_release(
    arguments: argparse.Namespace, archive: Path
) -> dict[str, Any]:
    """Publish one binding by digest and advance only its plugin-scoped index."""
    state = _prepare_runtime_binding_publish(arguments, archive)
    binding_publication = state.binding_publisher.publish(state.artifact)
    index_reference, index_digest, idempotent = _advance_runtime_binding_index(arguments, state)
    binding_signature = _signature_status(
        arguments,
        state.plugin_repository,
        state.artifact.digest,
    )
    index_signature = _signature_status(arguments, state.plugin_repository, index_digest)
    return {
        "archive": str(archive),
        "bindingIndexDigest": index_digest,
        "bindingIndexReference": index_reference,
        "bindingSignature": binding_signature,
        "bindingManifestDigest": binding_publication.manifest_digest,
        "channel": binding_publication.channel,
        "immutableReference": binding_publication.immutable_reference,
        "indexSignature": index_signature,
        "indexIdempotent": idempotent,
        "ok": True,
        "repository": binding_publication.repository,
    }


def _prepare_runtime_binding_publish(
    arguments: argparse.Namespace, archive: Path
) -> _RuntimeBindingPublishState:
    plugin_archive = getattr(arguments, "plugin_artifact_archive", None)
    if plugin_archive is None:
        raise SettingsError(
            "runtime-binding publish requires --plugin-artifact-archive so the binding can be verified against its plugin"
        )
    credentials = _publication_credentials(arguments, operation="runtime-binding publish")
    if not arguments.registry_origin or not arguments.repository:
        raise SettingsError(
            "runtime-binding publish requires registry.origin and artifactRepository"
        )
    if arguments.expires_in_hours is None or arguments.expires_in_hours <= 0:
        raise SettingsError(
            "runtime-binding publish requires catalog.expiresInHours for the binding index"
        )
    artifact, plugin_artifact, document = _read_linked_runtime_binding_archive(
        archive, plugin_archive, expected_plugin_repository=arguments.repository
    )
    plugin_link = _runtime_binding_plugin_link(document)
    plugin_repository = _required_string(
        plugin_link.get("repository"), "RuntimeBinding plugin repository"
    )
    plugin_digest = _required_string(plugin_link.get("digest"), "RuntimeBinding plugin digest")
    if plugin_artifact.digest != plugin_digest:
        raise ArtifactArchiveError(
            "RuntimeBinding does not reference the supplied plugin artifact digest"
        )

    binding_channel = f"binding-{artifact.digest.removeprefix('sha256:')}"
    binding_publisher = RuntimeBindingArtifactPublisher(
        PluginArtifactPublicationTarget(
            registry_origin=arguments.registry_origin,
            repository=plugin_repository,
            channel=binding_channel,
            allow_insecure_loopback_http=arguments.allow_insecure_loopback_http,
        ),
        credentials=credentials,
    )
    index_channel = _runtime_binding_index_channel(plugin_digest)
    index_publisher = RuntimeBindingIndexPublisher(
        CatalogIndexPublicationTarget(
            registry_origin=arguments.registry_origin,
            repository=plugin_repository,
            channel=index_channel,
            allow_insecure_loopback_http=arguments.allow_insecure_loopback_http,
        ),
        credentials=credentials,
        verify_current_index=_runtime_binding_index_verifier(arguments, plugin_repository),
        allow_unverified_current=arguments.allow_unverified_binding_index,
    )
    current = index_publisher.read_current()
    if (
        arguments.expected_binding_index_digest is not None
        and (current.manifest_digest if current is not None else None)
        != arguments.expected_binding_index_digest
    ):
        raise SettingsError(_EXPECTED_BINDING_INDEX_DIGEST_ERROR)
    return _RuntimeBindingPublishState(
        artifact,
        document,
        plugin_repository,
        plugin_digest,
        binding_publisher,
        index_publisher,
        current,
    )


def _advance_runtime_binding_index(
    arguments: argparse.Namespace, state: _RuntimeBindingPublishState
) -> tuple[str, str, bool]:
    current = state.current_index
    index_document, idempotent = next_runtime_binding_index(
        current=current.document if current is not None else None,
        current_manifest_digest=current.manifest_digest if current is not None else None,
        plugin_digest=state.plugin_digest,
        binding_repository=state.plugin_repository,
        binding_digest=state.artifact.digest,
        expires_in_hours=arguments.expires_in_hours,
        release=_mapping(state.document.get("metadata"), "RuntimeBinding metadata").get("release"),
    )
    if idempotent:
        assert current is not None
        index_digest = current.manifest_digest
        index_reference = f"{state.plugin_repository}@{index_digest}"
    else:
        index_artifact = build_runtime_binding_index_artifact(index_document)
        index_publication = state.index_publisher.publish(
            index_artifact,
            expected_current_digest=current.manifest_digest if current is not None else None,
            expected_current_etag=current.etag if current is not None else None,
        )
        index_digest = index_publication.manifest_digest
        index_reference = index_publication.immutable_reference
    return index_reference, index_digest, idempotent


def _signature_status(arguments: argparse.Namespace, repository: str, digest: str) -> str:
    reference = _cosign_immutable_reference(arguments, repository, digest)
    return "signed" if _sign_reference(arguments, reference) else "unsigned"


def _read_linked_runtime_binding_archive(
    binding_archive: Path,
    plugin_archive: Path,
    *,
    expected_plugin_repository: str,
) -> tuple[RuntimeBindingArtifact, PluginArtifact, dict[str, Any]]:
    """Verify a binding archive against the exact plugin archive it names."""
    plugin_artifact = read_plugin_artifact_archive(plugin_archive)
    plugin_descriptor = _decode_json_object(
        plugin_artifact.plugin_manifest.content, _PLUGIN_DESCRIPTOR_LABEL
    )
    binding_artifact = read_runtime_binding_archive(
        binding_archive,
        plugin_descriptor=plugin_descriptor,
        plugin_manifest_descriptor=plugin_artifact.manifest.descriptor,
    )
    binding_document = _decode_json_object(
        binding_artifact.binding_document.content, "RuntimeBinding document"
    )
    plugin_link = _mapping(
        _mapping(binding_document.get("spec"), _RUNTIME_BINDING_SPEC_LABEL).get("plugin"),
        _RUNTIME_BINDING_SPEC_PLUGIN_LABEL,
    )
    if plugin_link.get("repository") != expected_plugin_repository:
        raise ArtifactArchiveError("RuntimeBinding and plugin must use registry.artifactRepository")
    if plugin_link.get("digest") != plugin_artifact.digest:
        raise ArtifactArchiveError(
            "RuntimeBinding does not reference the supplied plugin artifact digest"
        )
    return binding_artifact, plugin_artifact, binding_document


def _runtime_binding_index_verifier(
    arguments: argparse.Namespace, repository: str
) -> Callable[[str], None]:
    """Build the trust check applied before a current binding index is extended."""
    verification_key = getattr(arguments, "cosign_verify_key", None)
    expected_digest = getattr(arguments, "expected_binding_index_digest", None)
    allow_unverified = bool(getattr(arguments, "allow_unverified_binding_index", False))

    def verify(digest: str) -> None:
        if expected_digest is not None and digest != expected_digest:
            raise SettingsError(_EXPECTED_BINDING_INDEX_DIGEST_ERROR)
        if verification_key is not None:
            verify_oci_reference(
                _cosign_immutable_reference(arguments, repository, digest),
                key=verification_key,
            )
        elif expected_digest is None and not allow_unverified:
            raise SettingsError(
                "extending a RuntimeBinding index requires --cosign-verify-key, "
                "--expected-binding-index-digest, or the development-only "
                "--allow-unverified-binding-index option"
            )

    return verify


def _update_catalog(arguments: argparse.Namespace) -> int:
    """Advance one catalog channel during recovery or release administration."""

    try:
        payload = _run_catalog_update(arguments)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (CatalogIndexPublicationError, ValueError) as error:
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
    except (CatalogIndexPublicationError, ValueError) as error:
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
    arguments: argparse.Namespace,
    *,
    artifact_path: Path | None = None,
    credentials: RegistryCredentials | None = None,
) -> dict[str, object]:
    credentials = credentials or _publication_credentials(arguments, operation="catalog update")
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
    signature = (
        "signed"
        if _sign_reference(
            arguments,
            _cosign_immutable_reference(
                arguments, arguments.catalog_repository, publication.manifest_digest
            ),
        )
        else "unsigned"
    )
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
        "signature": signature,
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
        immutable_reference = f"{arguments.catalog_repository}@{current.manifest_digest}"
        signature = (
            "signed"
            if _sign_reference(
                arguments,
                _cosign_immutable_reference(
                    arguments, arguments.catalog_repository, current.manifest_digest
                ),
            )
            else "unsigned"
        )
        return {
            "catalogIndexDigest": current.manifest_digest,
            "channel": arguments.catalog_channel,
            "entry": dict(entry),
            "generation": metadata["generation"],
            "idempotent": True,
            "immutableReference": immutable_reference,
            "ok": True,
            "repository": arguments.catalog_repository,
            "signature": signature,
        }
    index_artifact = build_catalog_index_artifact(document)
    _phase(arguments, "Publishing the catalog index")
    publication = publisher.publish(index_artifact)
    signature = (
        "signed"
        if _sign_reference(
            arguments,
            _cosign_immutable_reference(
                arguments, arguments.catalog_repository, publication.manifest_digest
            ),
        )
        else "unsigned"
    )
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
        "signature": signature,
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
        (auths[candidate] for candidate in candidates if isinstance(auths.get(candidate), Mapping)),
        None,
    )
    if not isinstance(record, Mapping) or not isinstance(record.get("auth"), str):
        raise SettingsError(f"registry auth file has no Basic credential for {host}")
    try:
        decoded = base64.b64decode(record["auth"], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (UnicodeDecodeError, ValueError, binascii.Error) as error:
        raise SettingsError(
            f"registry auth file has an invalid Basic credential for {host}"
        ) from error
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


def _publish_archive(
    arguments: argparse.Namespace,
    archive: Path,
    *,
    credentials: RegistryCredentials | None = None,
) -> dict[str, object]:
    """Verify and publish a portable archive using already-resolved coordinates."""

    credentials = credentials or _publication_credentials(arguments, operation="publish")
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


def _sign_reference(arguments: argparse.Namespace, reference: str) -> bool:
    """Sign an exact immutable registry identity when a key was explicitly supplied."""
    key = getattr(arguments, "cosign_key", None)
    if key is None:
        return False
    sign_oci_reference(reference, key=key)
    return True


def _cosign_immutable_reference(arguments: argparse.Namespace, repository: str, digest: str) -> str:
    """Build Cosign's fully-qualified registry reference from a repository and digest."""
    if _SHA256_DIGEST.fullmatch(digest) is None:
        raise SettingsError("Cosign signing requires a sha256 manifest digest")
    origin = urlparse(arguments.registry_origin)
    if not origin.hostname:
        raise SettingsError("Cosign signing requires a registry origin with a hostname")
    registry_host = origin.netloc
    normalized_repository = repository.strip("/")
    if normalized_repository.startswith(f"{registry_host}/"):
        remote_repository = normalized_repository
    else:
        remote_repository = f"{registry_host}/{normalized_repository}"
    return f"{remote_repository}@{digest}"


def _inspect(arguments: argparse.Namespace) -> int:
    """Inspect either source metadata or a verified portable artifact archive."""
    if arguments.verify:
        return _inspect_signature(arguments)
    if arguments.manifest.name == "release.json":
        return _inspect_release(arguments)
    if arguments.manifest.name.endswith(".oci.tar"):
        return _inspect_oci_archive(arguments)
    if arguments.manifest.suffix in {".yaml", ".yml"}:
        result = _inspect_runtime_binding_source(arguments)
        if result is not None:
            return result
    return _compile_workspace(arguments)


def _inspect_signature(arguments: argparse.Namespace) -> int:
    if arguments.manifest.exists():
        print(
            "error: Cosign verification requires an immutable remote OCI reference",
            file=sys.stderr,
        )
        return 2
    try:
        verify_oci_reference(str(arguments.manifest), key=arguments.cosign_key)
    except CosignError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"kind": "CosignVerification", "reference": str(arguments.manifest), "verified": True},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


def _inspect_release(arguments: argparse.Namespace) -> int:
    try:
        payload = dict(_read_release_inventory(arguments.manifest))
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    plugin = _mapping(payload["plugin"], _RELEASE_PLUGIN_LABEL)
    binding = _mapping(payload["runtimeBinding"], "release RuntimeBinding")
    print(f"Plugin: {plugin['repository']}@{plugin['digest']}")
    print(f"Plugin archive will publish: {plugin['publish']}")
    print(f"RuntimeBinding: {binding['repository']}@{binding['digest']}")
    print(f"Workload images: {len(payload['images'])}")
    for image in payload["images"]:
        print(f"  {image['workload']}/{image['buildTarget']}: {image['reference']}")
    return 0


def _inspect_oci_archive(arguments: argparse.Namespace) -> int:
    if arguments.image_binding:
        print(
            "error: --image-binding applies only when inspecting a source plugin manifest",
            file=sys.stderr,
        )
        return 2
    try:
        artifact = read_plugin_artifact_archive(arguments.manifest)
    except ArtifactArchiveError as plugin_error:
        return _inspect_non_plugin_archive(arguments.manifest, plugin_error)
    payload = _artifact_payload(artifact.plugin_manifest.descriptor.digest, artifact)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _inspect_non_plugin_archive(path: Path, plugin_error: ArtifactArchiveError) -> int:
    try:
        binding = read_runtime_binding_archive(path)
    except ArtifactArchiveError as binding_error:
        try:
            payload = inspect_workload_archive(path)
        except WorkloadBuildError as image_error:
            print(
                "error: not a plugin, RuntimeBinding, or supported workload image archive: "
                f"{plugin_error}; {binding_error}; {image_error}",
                file=sys.stderr,
            )
            return 1
    else:
        payload = {
            "artifactDigest": binding.digest,
            "bindingDocument": json.loads(binding.binding_document.content),
            "bindingDocumentDigest": binding.binding_document.descriptor.digest,
            "kind": "RuntimeBinding",
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _inspect_runtime_binding_source(arguments: argparse.Namespace) -> int | None:
    try:
        source = _read_runtime_binding_source(arguments.manifest)
    except SettingsError:
        return None
    if source.get("kind") != "RuntimeBinding":
        return None
    spec = _mapping(source.get("spec"), _RUNTIME_BINDING_SPEC_LABEL)
    resolved = "contract" in spec
    diagnostics = validate_runtime_binding(load_bundle(), source, resolved=resolved)
    if diagnostics:
        for diagnostic in diagnostics:
            print(f"{diagnostic.code}: {diagnostic.message}", file=sys.stderr)
        return 1
    payload = {"kind": "RuntimeBinding", "valid": True, "document": source}
    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(f"Valid RuntimeBinding: {arguments.manifest}")
        print("State: resolved" if resolved else "State: source")
    return 0


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


def _plugin_template(
    namespace: str,
    name: str,
    version: str,
    action_path: Path,
    *,
    custom_workload: bool = False,
) -> str:
    """Return a minimal plugin manifest for the selected init template."""
    workload = "  workloads:\n    python:\n      image: python-runtime\n" if custom_workload else ""
    return (
        f"apiVersion: {_API_VERSION}\n"
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
        f"{workload}"
        "  targets:\n"
        f"    - {action_path.as_posix()}\n"
    )


def _settings_template(
    name: str,
    *,
    namespace: str,
    version: str,
    custom_workload: bool = False,
) -> str:
    """Return plugin-root settings without duplicating manifest target selection."""
    base = (
        f"apiVersion: {_API_VERSION}\n"
        "kind: PluginBuildSettings\n"
        "manifest: plugin.yaml\n"
        "build:\n"
        f"  artifactOutput: dist/{name}.oci.tar\n"
    )
    if not custom_workload:
        return base
    return (
        base.rstrip("\n")
        + "\n"
        + f"  runtimeBindings: {_RUNTIME_BINDINGS_FILENAME}\n"
        + "  workloads:\n"
        + "    python-runtime:\n"
        + "      engine: podman\n"
        + "      context: .\n"
        + "      containerfile: Containerfile\n"
        + f"      repository: quay.io/{namespace}/{name}-runtime\n"
        + f"      tag: {name}-{version}\n"
        + "      platform: linux/amd64\n"
        + "registry:\n"
        + "  origin: https://quay.io\n"
        + f"  artifactRepository: {namespace}/{name}\n"
        + f"  workloadRepository: {namespace}/{name}-runtime\n"
        + "publish:\n"
        + f"  channel: {name}-{version}\n"
        + "catalog:\n"
        + "  sourceId: local-dev\n"
        + "  channel: stable\n"
        + "  expiresInHours: 168\n"
    )


def _action_template(
    action: str,
    input_path: Path,
    *,
    custom_workload: bool = False,
    package_name: str = "example_plugin",
) -> str:
    """Return a compilable example action referencing a registered implementation."""
    if custom_workload:
        return (
            f"apiVersion: {_API_VERSION}\n"
            "kind: Action\n"
            "metadata:\n"
            f"  name: {action}\n"
            f"  displayName: {_display_name(action)}\n"
            "  description: Demonstrates a publisher-owned custom workload.\n"
            "spec:\n"
            "  runtime:\n"
            "    kind: custom-runner\n"
            "    workload: python\n"
            f"    entryPoint: {package_name}.action:ExampleAction\n"
            "  input:\n"
            "    kind: inline\n"
            "    value:\n"
            "      type: object\n"
            "      additionalProperties: false\n"
            "      properties:\n"
            "        message:\n"
            "          type: string\n"
            "          minLength: 1\n"
            "      required: [message]\n"
            "  output:\n"
            "    kind: inline\n"
            "    value:\n"
            "      type: object\n"
            "      additionalProperties: false\n"
            "      properties:\n"
            "        result:\n"
            "          type: string\n"
            "      required: [result]\n"
            "  error:\n"
            "    kind: inline\n"
            "    value:\n"
            "      type: object\n"
            "      additionalProperties: true\n"
        )
    return (
        f"apiVersion: {_API_VERSION}\n"
        "kind: Action\n"
        "metadata:\n"
        f"  name: {action}\n"
        f"  displayName: {_display_name(action)}\n"
        "  description: Replace this example operation with a provider-specific action.\n"
        "spec:\n"
        "  runtime:\n"
        "    kind: platform-runner\n"
        "    implementation: syntara.http.request@1.0.0\n"
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


def _input_schema(field_names: Sequence[str]) -> dict[str, Any]:
    """Construct the scaffold schema without interpreting field names as YAML scalars."""
    return {
        "type": "object",
        "description": "Inputs accepted by this action.",
        "additionalProperties": False,
        "required": ["resource"],
        "properties": {
            field: {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": f"{'Required' if field == 'resource' else 'Optional'} input field.",
            }
            for field in ("resource", *field_names)
        },
    }


def _readme_template(name: str, action: str, *, custom_workload: bool = False) -> str:
    """Explain the generated files and their intentionally incomplete route."""
    if custom_workload:
        return (
            f"# {_display_name(name)}\n\n"
            "This workspace contains a Python custom-runner example. The plugin contract, "
            "runtime binding, and workload image have separate digests and release lifecycles.\n\n"
            f"Review and update `syntara-plugin.yaml`, `{_RUNTIME_BINDINGS_FILENAME}`, and the `Containerfile` "
            "before building. The `src/` package contains the workload implementation and the "
            "runtime's packaged manifest copy. Keep those packaged manifests aligned with the "
            "workspace manifests.\n\n"
            "```sh\n"
            "syntara-plugin validate plugin.yaml\n"
            "syntara-plugin run "
            f'{action} --mode source --input \'{{"message":"hello"}}\'\n'
            "syntara-plugin build\n"
            "syntara-plugin inspect dist/release.json\n"
            "```\n"
        )
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


def _write_custom_workload_scaffold(
    directory: Path,
    *,
    name: str,
    namespace: str,
    version: str,
    package_name: str,
    action_path: Path,
    action_manifest: str,
) -> None:
    """Write a small Python runtime, image recipe, and binding source manifest."""
    from .custom_scaffold import DEFAULT_RUNTIME_BASE_IMAGE

    binding_name = f"{name[:54].rstrip('-')}-runtime"
    package = directory / "src" / package_name
    manifests = package / "manifests"
    (manifests / action_path.parent).mkdir(parents=True)
    (package / "__init__.py").write_text(
        '"""Custom workload implementation."""\n', encoding="utf-8"
    )
    (package / "action.py").write_text(
        """from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from syntara_plugin.runtime import ActionStep, ExecutionContext


class ExampleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str


class ExampleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: str


class ExampleAction(ActionStep[ExampleInput, ExampleOutput]):
    def run(self, inputs: ExampleInput, context: ExecutionContext) -> ExampleOutput:
        return ExampleOutput(result=inputs.message)
""",
        encoding="utf-8",
    )
    (package / "runtime.py").write_text(
        "from syntara_plugin.runtime import load_plugin_bindings\n\n"
        "def create_bindings():\n"
        f'    return load_plugin_bindings({package_name!r}, workload_id="python")\n',
        encoding="utf-8",
    )
    (manifests / "plugin.yaml").write_text(
        _plugin_template(
            namespace,
            name,
            version,
            action_path,
            custom_workload=True,
        ),
        encoding="utf-8",
    )
    (manifests / action_path).write_text(action_manifest, encoding="utf-8")
    (manifests / "docs").mkdir(parents=True, exist_ok=True)
    (manifests / "docs" / "README.md").write_text(
        f"# {_display_name(name)}\n\nGenerated custom workload example.\n", encoding="utf-8"
    )
    (directory / _RUNTIME_BINDINGS_FILENAME).write_text(
        f"apiVersion: {_API_VERSION}\n"
        "kind: RuntimeBinding\n"
        "metadata:\n"
        f"  namespace: {namespace}\n  name: {binding_name}\n"
        "spec:\n"
        "  plugin:\n    source: plugin.yaml\n"
        "  workloads:\n"
        "    python:\n"
        "      default: python-base\n"
        "      options:\n"
        "        - name: python-base\n"
        "          displayName: Python base\n"
        "          image:\n"
        "            buildTarget: python-runtime\n",
        encoding="utf-8",
    )
    (directory / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
        'requires-python = ">=3.12"\ndependencies = ["syntara-plugin-contracts==0.1.0a3"]\n\n'
        '[build-system]\nrequires = ["hatchling>=1.25"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[tool.hatch.build.targets.wheel]\n"
        f'packages = ["src/{package_name}"]\n',
        encoding="utf-8",
    )
    (directory / "runtime.json").write_text(
        '{\n  "abi": "syntara.container/v1alpha1",\n  "adapter": {\n'
        f'    "bindingsFactory": "{package_name}.runtime:create_bindings"\n'
        "  }\n}\n",
        encoding="utf-8",
    )
    (directory / "Containerfile").write_text(
        f"ARG SYNTARA_RUNTIME_BASE_IMAGE={DEFAULT_RUNTIME_BASE_IMAGE}\n"
        "FROM ${SYNTARA_RUNTIME_BASE_IMAGE}\n"
        "WORKDIR /opt/app\n"
        "USER 0\nRUN mkdir -p /etc/syntara\n"
        "COPY pyproject.toml ./\n"
        "COPY src ./src\n"
        "RUN python3.12 -m pip install --no-cache-dir .\n"
        "COPY runtime.json /etc/syntara/runtime.json\n"
        "USER 1001\n"
        "ENV PYTHONPATH=/opt/app/src \\\n"
        "    SYNTARA_RUNTIME_CONFIG=/etc/syntara/runtime.json \\\n"
        "    SYNTARA_RUNTIME_GRPC_ADDRESS=0.0.0.0:50051 \\\n"
        "    SYNTARA_RUNTIME_GRPC_TLS_CERT=/run/secrets/server.crt \\\n"
        "    SYNTARA_RUNTIME_GRPC_TLS_KEY=/run/secrets/server.key \\\n"
        "    SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA=/run/secrets/client-ca.crt \\\n"
        "    PYTHONUNBUFFERED=1 \\\n"
        "    PYTHONDONTWRITEBYTECODE=1\n"
        "EXPOSE 50051\n"
        'ENTRYPOINT ["python3.12", "-m", "syntara_plugin.runtime.server"]\n',
        encoding="utf-8",
    )


def _display_name(value: str) -> str:
    """Turn a valid identifier into a human-readable scaffold label."""
    return " ".join(part.capitalize() for part in value.split("-"))


if __name__ == "__main__":
    raise SystemExit(main())
