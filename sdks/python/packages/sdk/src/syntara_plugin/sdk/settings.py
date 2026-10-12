"""Versioned, non-secret settings for the Syntara plugin authoring CLI.

Settings reduce repetitive destination and container-build arguments without
turning a local configuration file into a credential store.  Invocation-only
behaviour such as reading a password from standard input stays outside this
model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml
from syntara_plugin.contracts import load_bundle, validate_document


SETTINGS_API_VERSION = "syntara.io/v1alpha1"
SETTINGS_KIND = "PluginBuildSettings"
SETTINGS_FILENAME = "syntara-plugin.yaml"


class SettingsError(ValueError):
    """Raised when CLI settings are absent, unsafe, or do not match the contract."""


@dataclass(frozen=True)
class WorkloadBuildSettings:
    """One explicit, non-secret Podman workload-image build target."""

    engine: str | None = None
    context: Path | None = None
    containerfile: Path | None = None
    repository: str | None = None
    tag: str | None = None
    platform: str | None = None
    image_id: str | None = None


@dataclass(frozen=True)
class RegistrySettings:
    """One registry identity shared by workload, artifact, and catalog publication."""

    origin: str | None = None
    artifact_repository: str | None = None
    workload_repository: str | None = None
    catalog_repository: str | None = None
    username: str | None = None
    auth_file: Path | None = None
    allow_insecure_loopback_http: bool | None = None


@dataclass(frozen=True)
class BuildSettings:
    """Source compilation and portable metadata-archive defaults."""

    artifact_output: Path | None = None
    image_bindings: Mapping[str, str] = field(default_factory=dict)
    workload: WorkloadBuildSettings | None = None


@dataclass(frozen=True)
class OutputSettings:
    """Presentation defaults shared by every command that produces output."""

    verbose: bool | None = None
    json: bool | None = None


@dataclass(frozen=True)
class PublishSettings:
    """Metadata-artifact publication settings that are not registry identity."""

    artifact: Path | None = None
    channel: str | None = None
    update_catalog: bool = True


@dataclass(frozen=True)
class CatalogSettings:
    """Catalog ownership and freshness defaults for one release channel."""

    source_id: str | None = None
    channel: str | None = None
    expires_in_hours: int | None = None


@dataclass(frozen=True)
class PluginBuildSettings:
    """The complete language-neutral CLI settings document."""

    source: Path | None = None
    manifest: Path | None = None
    output: OutputSettings = field(default_factory=OutputSettings)
    registry: RegistrySettings = field(default_factory=RegistrySettings)
    build: BuildSettings = field(default_factory=BuildSettings)
    publish: PublishSettings = field(default_factory=PublishSettings)
    catalog: CatalogSettings = field(default_factory=CatalogSettings)


def load_settings(*, explicit_path: Path | None, manifest: Path | None) -> PluginBuildSettings:
    """Load the first settings document selected by the stable discovery order.

    The command line always wins at a later resolution stage.  This function
    deliberately does not merge files. A selected plugin manifest owns its
    adjacent configuration, so an unrelated working-directory settings file
    cannot change that plugin's release coordinates.
    """

    source = _select_settings_path(explicit_path=explicit_path, manifest=manifest)
    if source is None:
        return PluginBuildSettings()
    return _parse_settings(source)


def _select_settings_path(*, explicit_path: Path | None, manifest: Path | None) -> Path | None:
    if explicit_path is not None:
        candidate = explicit_path.expanduser()
        if not candidate.is_file():
            raise SettingsError(
                f"settings file does not exist or is not a regular file: {candidate}"
            )
        return candidate.resolve()

    if manifest is not None:
        manifest_candidate = manifest.expanduser().parent / SETTINGS_FILENAME
        if manifest_candidate.is_file():
            return manifest_candidate.resolve()

    working_directory_candidate = Path.cwd() / SETTINGS_FILENAME
    if working_directory_candidate.is_file():
        return working_directory_candidate.resolve()
    return None


def _parse_settings(source: Path) -> PluginBuildSettings:
    try:
        loaded = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise SettingsError(f"could not read settings file {source}: {error}") from error
    document = _mapping(loaded, "settings document")
    _reject_secrets(document)
    _reject_unknown_keys(
        document,
        {
            "apiVersion",
            "kind",
            "manifest",
            "output",
            "registry",
            "build",
            "publish",
            "catalog",
        },
        "settings document",
    )
    if document.get("apiVersion") != SETTINGS_API_VERSION:
        raise SettingsError(f"settings document apiVersion must be {SETTINGS_API_VERSION!r}")
    if document.get("kind") != SETTINGS_KIND:
        raise SettingsError(f"settings document kind must be {SETTINGS_KIND!r}")

    parsed = PluginBuildSettings(
        source=source,
        manifest=_path(document.get("manifest"), source, "manifest"),
        output=_parse_output(document.get("output")),
        registry=_parse_registry(document.get("registry"), source),
        build=_parse_build(document.get("build"), source),
        publish=_parse_publish(document.get("publish"), source),
        catalog=_parse_catalog(document.get("catalog")),
    )
    schema_errors = validate_document(load_bundle(), "plugin_build_settings", document)
    if schema_errors:
        raise SettingsError("settings document schema validation failed: " + "; ".join(schema_errors))
    return parsed


def _parse_registry(value: object, source: Path) -> RegistrySettings:
    if value is None:
        return RegistrySettings()
    document = _mapping(value, "registry")
    _reject_unknown_keys(
        document,
        {
            "origin",
            "artifactRepository",
            "workloadRepository",
            "catalogRepository",
            "username",
            "authFile",
            "allowInsecureLoopbackHttp",
        },
        "registry",
    )
    allow_insecure = document.get("allowInsecureLoopbackHttp")
    if allow_insecure is not None and not isinstance(allow_insecure, bool):
        raise SettingsError("registry.allowInsecureLoopbackHttp must be a boolean")
    return RegistrySettings(
        origin=_string(document.get("origin"), "registry.origin"),
        artifact_repository=_string(
            document.get("artifactRepository"), "registry.artifactRepository"
        ),
        workload_repository=_string(
            document.get("workloadRepository"), "registry.workloadRepository"
        ),
        catalog_repository=_string(
            document.get("catalogRepository"), "registry.catalogRepository"
        ),
        username=_string(document.get("username"), "registry.username"),
        auth_file=_path(document.get("authFile"), source, "registry.authFile"),
        allow_insecure_loopback_http=allow_insecure,
    )


def _parse_output(value: object) -> OutputSettings:
    if value is None:
        return OutputSettings()
    document = _mapping(value, "output")
    _reject_unknown_keys(document, {"verbose", "json"}, "output")
    return OutputSettings(
        verbose=_boolean(document.get("verbose"), "output.verbose"),
        json=_boolean(document.get("json"), "output.json"),
    )


def _parse_build(value: object, source: Path) -> BuildSettings:
    if value is None:
        return BuildSettings()
    document = _mapping(value, "build")
    _reject_unknown_keys(
        document,
        {"artifactOutput", "imageBindings", "workload"},
        "build",
    )
    raw_bindings = document.get("imageBindings", {})
    bindings = _mapping(raw_bindings, "build.imageBindings")
    parsed_bindings: dict[str, str] = {}
    for identifier, image in bindings.items():
        if not isinstance(identifier, str) or not isinstance(image, str) or not image:
            raise SettingsError("build.imageBindings must map non-empty image IDs to references")
        parsed_bindings[identifier] = image
    return BuildSettings(
        artifact_output=_path(
            document.get("artifactOutput"), source, "build.artifactOutput"
        ),
        image_bindings=parsed_bindings,
        workload=_parse_workload(document.get("workload"), source),
    )


def _parse_workload(value: object, source: Path) -> WorkloadBuildSettings | None:
    if value is None:
        return None
    document = _mapping(value, "build.workload")
    _reject_unknown_keys(
        document,
        {"engine", "context", "containerfile", "tag", "platform", "imageId"},
        "build.workload",
    )
    return WorkloadBuildSettings(
        engine=_string(document.get("engine"), "build.workload.engine"),
        context=_path(document.get("context"), source, "build.workload.context"),
        containerfile=_path(document.get("containerfile"), source, "build.workload.containerfile"),
        tag=_string(document.get("tag"), "build.workload.tag"),
        platform=_string(document.get("platform"), "build.workload.platform"),
        image_id=_string(document.get("imageId"), "build.workload.imageId"),
    )


def _parse_publish(value: object, source: Path) -> PublishSettings:
    if value is None:
        return PublishSettings()
    document = _mapping(value, "publish")
    _reject_unknown_keys(
        document,
        {"artifact", "channel", "updateCatalog"},
        "publish",
    )
    update_catalog = _boolean(document.get("updateCatalog"), "publish.updateCatalog")
    return PublishSettings(
        artifact=_path(document.get("artifact"), source, "publish.artifact"),
        channel=_string(document.get("channel"), "publish.channel"),
        update_catalog=True if update_catalog is None else update_catalog,
    )


def _parse_catalog(value: object) -> CatalogSettings:
    if value is None:
        return CatalogSettings()
    document = _mapping(value, "catalog")
    _reject_unknown_keys(document, {"sourceId", "channel", "expiresInHours"}, "catalog")
    expires_in_hours = document.get("expiresInHours")
    if (
        expires_in_hours is not None
        and (not isinstance(expires_in_hours, int) or isinstance(expires_in_hours, bool))
    ):
        raise SettingsError("catalog.expiresInHours must be a positive integer")
    if expires_in_hours is not None and expires_in_hours <= 0:
        raise SettingsError("catalog.expiresInHours must be a positive integer")
    return CatalogSettings(
        source_id=_string(document.get("sourceId"), "catalog.sourceId"),
        channel=_string(document.get("channel"), "catalog.channel"),
        expires_in_hours=expires_in_hours,
    )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SettingsError(f"{label} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise SettingsError(f"{label} must use string keys")
    return value


def _reject_unknown_keys(document: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise SettingsError(f"{label} contains unsupported setting(s): {', '.join(unknown)}")


def _reject_secrets(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if normalized in {
                "password",
                "accesstoken",
                "token",
                "secret",
                "privatekey",
            }:
                raise SettingsError(f"settings must not contain secret value {path}.{key}")
            _reject_secrets(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_secrets(nested, f"{path}[{index}]")


def _path(value: object, source: Path, label: str) -> Path | None:
    text = _string(value, label)
    if text is None:
        return None
    candidate = Path(text).expanduser()
    return candidate if candidate.is_absolute() else (source.parent / candidate).resolve()


def _string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SettingsError(f"{label} must be a non-empty string")
    return value


def _boolean(value: object, label: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise SettingsError(f"{label} must be a boolean")
    return value
