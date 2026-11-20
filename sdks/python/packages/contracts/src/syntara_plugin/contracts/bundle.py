"""Load and validate the self-contained v1alpha1 contract bundle."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource

ContractKind = Literal[
    "plugin",
    "plugin_build_settings",
    "action",
    "control",
    "trigger",
    "step",
    "plugin_descriptor",
    "registration_manifest",
    "resolved_execution",
    "runtime",
    "provider",
    "catalog_index",
    "catalog_source",
    "catalog_source_status",
    "integration",
    "http_mapping",
    "http_runner_context",
    "credential_recipe",
    "trigger_driver",
    "trigger_event_mapping",
    "installed_identity",
    "runtime_binding_source",
    "runtime_binding_resolved",
    "runtime_binding_index",
]
_SCHEMA_FILES: dict[ContractKind, str] = {
    "plugin": "plugin.schema.json",
    "plugin_build_settings": "plugin-build-settings.schema.json",
    "action": "action.schema.json",
    "control": "control.schema.json",
    "trigger": "trigger.schema.json",
    "step": "step.schema.json",
    "plugin_descriptor": "plugin-descriptor.schema.json",
    "registration_manifest": "registration-manifest.schema.json",
    "resolved_execution": "resolved-execution.schema.json",
    "runtime": "runtime.schema.json",
    "provider": "provider.schema.json",
    "catalog_index": "catalog-index.schema.json",
    "catalog_source": "catalog-source.schema.json",
    "catalog_source_status": "catalog-source-status.schema.json",
    "integration": "integration.schema.json",
    "http_mapping": "http-operation.schema.json",
    "http_runner_context": "http-runner-context.schema.json",
    "credential_recipe": "credential-recipe.schema.json",
    "trigger_driver": "trigger-driver.schema.json",
    "trigger_event_mapping": "trigger-event-mapping.schema.json",
    "installed_identity": "installed-identity.schema.json",
    "runtime_binding_source": "runtime-binding-source.schema.json",
    "runtime_binding_resolved": "runtime-binding-resolved.schema.json",
    "runtime_binding_index": "runtime-binding-index.schema.json",
}
_RFC3339_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
# Start from an empty registry so only the contract-asserted format names are
# checked; jsonschema's built-in checks (email, date, ...) would otherwise be
# asserted too, which the authoring conformance contract does not allow.
_FORMAT_CHECKER = FormatChecker(formats=())
_STEP_TARGET_KINDS = frozenset({"Action", "Control", "Trigger"})
_UTC_OFFSET_SUFFIX = "+00:00"
_SCHEMA_KEYWORD = "$schema"
_CANONICAL_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_SCHEMA_VALIDATION_LOCK = Lock()


def _asserted_formats(documents: Mapping[str, Mapping[str, Any]]) -> frozenset[str]:
    """Read the asserted format names declared by the common contract."""

    try:
        enum = documents["common.schema.json"]["$defs"]["assertedFormats"]["enum"]
    except (KeyError, TypeError) as error:
        raise RuntimeError(
            "common.schema.json is missing the $defs/assertedFormats/enum declaration"
        ) from error
    if not isinstance(enum, list) or not all(isinstance(name, str) for name in enum):
        raise RuntimeError("assertedFormats enum must be a list of format names")
    return frozenset(enum)


@_FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _is_rfc3339_datetime(value: object) -> bool:
    """Accept the RFC 3339 subset used by Syntara runtime envelopes.

    Per the Draft 2020-12 format-assertion vocabulary, a format only applies
    to string instances; non-strings succeed.
    """

    if not isinstance(value, str):
        return True
    if not _RFC3339_DATETIME.fullmatch(value):
        return False
    datetime.fromisoformat(
        value.removesuffix("Z") + _UTC_OFFSET_SUFFIX if value.endswith("Z") else value
    )
    return True


# RFC 3986: scheme = ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ).
_URI_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")
# Characters RFC 3986 does not allow anywhere in a URI: ASCII controls,
# space, and the delimiters excluded from every component grammar.
_URI_FORBIDDEN = re.compile(r"[\x00-\x20\x7f<>\"{}|\\^`]")
# A "%" must introduce a valid percent-encoded octet.
_MALFORMED_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
# RFC 3986 component grammars. "[]" is legal only inside an IPv6 host
# literal, so it is absent from every other component's character class.
_PCT_ENCODED = r"%[0-9A-Fa-f]{2}"
_UNRESERVED = r"A-Za-z0-9\-._~"
_SUB_DELIMS = r"!$&'()*+,;="
# pchar = unreserved / pct-encoded / sub-delims / ":" / "@"; a path adds "/".
_URI_PATH = re.compile(rf"(?:[{_UNRESERVED}{_SUB_DELIMS}:@/]|{_PCT_ENCODED})*")
# query and fragment add "/" and "?" to pchar.
_URI_QUERY_OR_FRAGMENT = re.compile(rf"(?:[{_UNRESERVED}{_SUB_DELIMS}:@/?]|{_PCT_ENCODED})*")
# userinfo = *( unreserved / pct-encoded / sub-delims / ":" ).
_URI_USERINFO = re.compile(rf"(?:[{_UNRESERVED}{_SUB_DELIMS}:]|{_PCT_ENCODED})*")
# reg-name = *( unreserved / pct-encoded / sub-delims ).
_URI_REG_NAME = re.compile(rf"(?:[{_UNRESERVED}{_SUB_DELIMS}]|{_PCT_ENCODED})*")
# IP-literal brackets are only valid around an IPv6 address or an IPvFuture
# literal (RFC 3986 section 3.2.2: "v" 1*HEXDIG "." 1*(unreserved /
# sub-delims / ":")).
_URI_IPV6_LITERAL = re.compile(r"\[[0-9A-Fa-f:.]+\]")
_URI_IPVFUTURE = re.compile(rf"v[0-9A-Fa-f]+\.[{_UNRESERVED}{_SUB_DELIMS}:]+")


@_FORMAT_CHECKER.checks("uri", raises=ValueError)
def _is_absolute_uri(value: object) -> bool:
    """Accept absolute RFC 3986 URIs: a valid scheme plus a valid remainder.

    Every component is checked against its RFC 3986 grammar, including the
    authority (userinfo, host, port) and the path, query, and fragment
    character sets. Non-strings succeed, per the Draft 2020-12
    format-assertion vocabulary.
    """

    if not isinstance(value, str):
        return True
    # urlsplit raises ValueError for inputs it cannot parse (for example a
    # malformed IPv6 host); the registered ``raises`` turns that into a failed
    # format check instead of an escaping exception. The port grammar itself
    # is checked by _valid_uri_authority: urlsplit's .port accessor applies a
    # numeric range limit that RFC 3986 section 3.2.3 does not, so it is not
    # consulted here.
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    if not parsed.scheme or not _URI_SCHEME.fullmatch(parsed.scheme):
        return False
    if not value.isascii() or _URI_FORBIDDEN.search(value) or _MALFORMED_PERCENT.search(value):
        return False
    if parsed.netloc and not _valid_uri_authority(parsed.netloc):
        return False
    if not _URI_PATH.fullmatch(parsed.path):
        return False
    if not _URI_QUERY_OR_FRAGMENT.fullmatch(parsed.query):
        return False
    if not _URI_QUERY_OR_FRAGMENT.fullmatch(parsed.fragment):
        return False
    return True


def _valid_uri_authority(netloc: str) -> bool:
    """Validate one RFC 3986 authority: optional userinfo, host, and port."""

    userinfo, _, hostport = netloc.rpartition("@")
    if not _URI_USERINFO.fullmatch(userinfo):
        return False
    if hostport.startswith("["):
        closing = hostport.find("]")
        if closing == -1:
            return False
        literal, port = hostport[: closing + 1], hostport[closing + 1 :]
        if not (
            _URI_IPV6_LITERAL.fullmatch(literal) or _URI_IPVFUTURE.fullmatch(literal[1:-1])
        ) or not (port in ("", ":") or port[1:].isdigit()):
            return False
        return True
    host, separator, port = hostport.rpartition(":")
    if not separator:
        host, port = hostport, ""
    if not _URI_REG_NAME.fullmatch(host):
        return False
    return port == "" or port.isdigit()


def format_checker() -> FormatChecker:
    """Return the shared format checker asserting the contract format set.

    Only the names declared in ``common.schema.json#/$defs/assertedFormats``
    are registered; every other format name is an annotation only, per JSON
    Schema Draft 2020-12 vocabulary semantics. The checker is a process-wide
    singleton: callers must treat it as read-only and never register
    additional checks on it.
    """

    return _FORMAT_CHECKER


def build_validator(schema: object, *, registry: Registry | None = None) -> Draft202012Validator:
    """Build a Draft 2020-12 validator with the shared asserted format checker."""

    return Draft202012Validator(
        schema,
        registry=registry if registry is not None else Registry(),
        format_checker=_FORMAT_CHECKER,
    )


class SchemaDocumentError(ValueError):
    """An authored schema document violates the authoring conformance contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _reject_embedded_dialect_declarations(value: object, path: str) -> None:
    """Reject nested ``$schema`` declarations that switch the dialect.

    An embedded resource (for example a ``$defs`` entry with its own ``$id``)
    that declares another ``$schema`` would be evaluated with that dialect's
    semantics when referenced, silently bypassing the root-only draft check.
    """

    if isinstance(value, Mapping):
        declared = value.get(_SCHEMA_KEYWORD)
        if isinstance(declared, str) and declared != _CANONICAL_SCHEMA_DIALECT:
            raise SchemaDocumentError(
                "SCHEMA_EMBEDDED_DIALECT_UNSUPPORTED",
                f"embedded schema resource at {path!r} declares unsupported "
                f"dialect {declared!r}; every schema resource must use "
                f"{_CANONICAL_SCHEMA_DIALECT!r}.",
            )
        for key, item in value.items():
            _reject_embedded_dialect_declarations(item, f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_embedded_dialect_declarations(item, f"{path}/{index}")


def validate_authored_schema(document: object) -> None:
    """Validate an authored (inline or asset-backed) schema document.

    Requires an explicit Draft 2020-12 ``$schema`` declaration and a document
    that satisfies the Draft 2020-12 meta-schema. The declaration is enforced
    on the root and on every embedded resource: a nested ``$schema`` pointing
    at another draft would make referenced subresources evaluate under that
    dialect's semantics. Raises ``SchemaDocumentError`` with a stable code for
    draft violations and jsonschema ``SchemaError`` for structural meta-schema
    failures.
    """

    if not isinstance(document, Mapping):
        raise SchemaDocumentError(
            "SCHEMA_DOCUMENT_NOT_OBJECT",
            "authored schema document must be a JSON object.",
        )
    declared = document.get(_SCHEMA_KEYWORD)
    if not isinstance(declared, str) or not declared:
        raise SchemaDocumentError(
            "SCHEMA_DOCUMENT_DIALECT_MISSING",
            "authored schema must declare $schema as the Draft 2020-12 dialect "
            f"{_CANONICAL_SCHEMA_DIALECT!r}.",
        )
    if declared != _CANONICAL_SCHEMA_DIALECT:
        raise SchemaDocumentError(
            "SCHEMA_DOCUMENT_UNSUPPORTED_DRAFT",
            f"authored schema declares unsupported dialect {declared!r}; only "
            f"{_CANONICAL_SCHEMA_DIALECT!r} is accepted.",
        )
    _reject_embedded_dialect_declarations(document, "<root>")
    Draft202012Validator.check_schema(document)


# Representative ErrorResult documents the SDK itself emits inside wrapper
# errors: the INVALID_INPUT wrapper produced before the step hook runs, and
# the default StepFailure shape an author gets without an explicit code.
# Messages are runtime-dependent, so these carry representative texts.
_WRAPPER_ERROR_DOCUMENTS: tuple[dict[str, Any], ...] = (
    {
        "category": "validation",
        "code": "INVALID_INPUT",
        "message": "The step input is invalid.",
        "retryable": False,
    },
    {
        "category": "business",
        "code": "STEP_FAILED",
        "message": "The step failed.",
        "retryable": False,
    },
)


def validate_declared_error_schema(document: Mapping[str, Any]) -> None:
    """Reject a declared ``spec.error`` that cannot carry an SDK wrapper error.

    Every wrapper error — including the INVALID_INPUT wrapper emitted before
    the step hook runs, and the default STEP_FAILED shape of ``StepFailure`` —
    is validated against the declared ``spec.error`` at invocation time. A
    schema that cannot carry those shapes would turn a routine invalid-input
    report into an opaque execution error, so authoring rejects it up front.
    Raises ``SchemaDocumentError`` with a stable code.
    """

    for wrapper in _WRAPPER_ERROR_DOCUMENTS:
        try:
            admits = build_validator(document).is_valid(wrapper)
        except Exception:
            # A schema that cannot even be evaluated (for example an
            # unresolvable $ref) is not a wrapper-admission failure; callers
            # report structural and reference errors through their own paths.
            continue
        if not admits:
            codes = ", ".join(repr(item["code"]) for item in _WRAPPER_ERROR_DOCUMENTS)
            raise SchemaDocumentError(
                "ERROR_SCHEMA_REJECTS_WRAPPER_ERROR",
                "declared spec.error cannot carry the SDK wrapper errors "
                f"({codes}); it must admit them, for example by allowing those "
                "codes alongside the step type's own.",
            )


@dataclass(frozen=True)
class ContractBundle:
    """An immutable in-process view of the packaged contract documents."""

    version: str
    digest: str
    documents: dict[str, dict[str, Any]]
    asserted_formats: frozenset[str] = frozenset()

    def validator(self, kind: ContractKind) -> Draft202012Validator:
        """Return a validator with every local schema in this bundle registered."""
        target = self.documents[_SCHEMA_FILES[kind]]
        registry = Registry().with_resources(
            (document["$id"], Resource.from_contents(document))
            for document in self.documents.values()
        )
        return build_validator(target, registry=registry)


def _schema_directory() -> Any:
    packaged = files("syntara_plugin.contracts").joinpath("schemas", "v1alpha1")
    if packaged.is_dir():
        return packaged
    # Editable source installs consume the repository's language-neutral source.
    for parent in Path(__file__).resolve().parents:
        source = parent / "contracts" / "schemas" / "v1alpha1"
        if source.is_dir():
            return source
    raise RuntimeError("cannot locate packaged or repository contract schemas")


def _canonical_bytes(documents: dict[str, dict[str, Any]]) -> bytes:
    payload = {path: documents[path] for path in sorted(documents)}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def bundle_digest(documents: dict[str, dict[str, Any]]) -> str:
    """Return the stable SHA-256 digest of a complete contract bundle."""
    return "sha256:" + hashlib.sha256(_canonical_bytes(documents)).hexdigest()


@lru_cache(maxsize=8)
def _validate_bundle_schemas(canonical: bytes) -> None:
    """Check each distinct bundle's schemas, caching only successful checks."""
    documents: dict[str, dict[str, Any]] = json.loads(canonical)
    for name, document in documents.items():
        if document.get(_SCHEMA_KEYWORD) != _CANONICAL_SCHEMA_DIALECT:
            raise RuntimeError(
                f"contract bundle document {name!r} must declare the "
                f"{_CANONICAL_SCHEMA_DIALECT!r} dialect"
            )
        try:
            Draft202012Validator.check_schema(document)
        except SchemaError as error:
            raise RuntimeError(
                f"contract bundle document {name!r} is not a valid "
                f"Draft 2020-12 schema: {error}"
            ) from error


def load_bundle() -> ContractBundle:
    """Load the packaged, local-only `syntara.io/v1alpha1` contract bundle."""
    directory = _schema_directory()
    documents: dict[str, dict[str, Any]] = {}
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if entry.name.endswith(".schema.json"):
            documents[entry.name] = json.loads(entry.read_text(encoding="utf-8"))
    if set(_SCHEMA_FILES.values()) - set(documents):
        raise RuntimeError("contract bundle is missing a required manifest or runtime schema")
    canonical = _canonical_bytes(documents)
    # lru_cache permits concurrent misses to execute the wrapped function more than once.
    with _SCHEMA_VALIDATION_LOCK:
        _validate_bundle_schemas(canonical)
    asserted_formats = _asserted_formats(documents)
    registered_formats = frozenset(_FORMAT_CHECKER.checkers)
    if asserted_formats != registered_formats:
        undeclared = sorted(registered_formats - asserted_formats)
        unregistered = sorted(asserted_formats - registered_formats)
        raise RuntimeError(
            "asserted format set and registered format checks disagree: "
            f"declared without a check: {undeclared or 'none'}; "
            f"registered without a declaration: {unregistered or 'none'}"
        )
    return ContractBundle(
        version="syntara.io/v1alpha1",
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        documents=documents,
        asserted_formats=asserted_formats,
    )


def validate_document(bundle: ContractBundle, kind: ContractKind, document: object) -> list[str]:
    """Return stable, path-prefixed JSON Schema validation errors."""
    errors = sorted(
        bundle.validator(kind).iter_errors(document), key=lambda error: list(error.path)
    )
    return [
        f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in errors
    ]


@dataclass(frozen=True)
class CatalogCheckpoint:
    """Last accepted immutable catalog state for one configured source.

    The caller persists this state. The contracts package only compares a
    candidate index to it; it does not fetch, verify signatures, or write data.
    """

    generation: int = 0
    index_digest: str | None = None


@dataclass(frozen=True)
class RuntimeBindingCheckpoint:
    """Last accepted runtime-binding index state for one plugin artifact."""

    generation: int = 0
    index_digest: str | None = None


@dataclass(frozen=True, order=True)
class RuntimeBindingDiagnostic:
    """Stable, non-secret reason a runtime binding or index was rejected."""

    code: str
    path: str
    message: str


def validate_runtime_binding(
    bundle: ContractBundle,
    document: Mapping[str, Any],
    *,
    resolved: bool,
    plugin_descriptor: Mapping[str, Any] | None = None,
) -> tuple[RuntimeBindingDiagnostic, ...]:
    """Validate a source or resolved binding and its links to a compiled plugin.

    This function checks document contracts and identities only. It does not
    resolve registries, verify signatures, or authorize installation.
    """

    kind: ContractKind = "runtime_binding_resolved" if resolved else "runtime_binding_source"
    schema_errors = validate_document(bundle, kind, document)
    diagnostics = [
        RuntimeBindingDiagnostic(
            code="RUNTIME_BINDING_SCHEMA_INVALID",
            path=message.partition(":")[0],
            message=message,
        )
        for message in schema_errors
    ]
    if schema_errors:
        return tuple(sorted(diagnostics))

    spec = document["spec"]
    diagnostics.extend(_runtime_binding_workload_errors(spec["workloads"], resolved))

    if plugin_descriptor is not None:
        diagnostics.extend(
            _runtime_binding_plugin_link_errors(document, plugin_descriptor, resolved)
        )
    return tuple(sorted(diagnostics))


def _runtime_binding_workload_errors(
    workloads: Mapping[str, Any], resolved: bool
) -> list[RuntimeBindingDiagnostic]:
    diagnostics: list[RuntimeBindingDiagnostic] = []
    for workload_id, workload in workloads.items():
        options = workload["options"]
        names = [option["name"] for option in options]
        if len(names) != len(set(names)):
            diagnostics.append(
                RuntimeBindingDiagnostic(
                    "RUNTIME_BINDING_OPTION_DUPLICATE",
                    f"spec/workloads/{workload_id}/options",
                    f"workload {workload_id!r} has duplicate option names.",
                )
            )
        for option in options:
            diagnostics.extend(_runtime_binding_option_errors(workload_id, option, resolved))
        if workload["default"] not in names:
            diagnostics.append(
                RuntimeBindingDiagnostic(
                    "RUNTIME_BINDING_DEFAULT_MISSING",
                    f"spec/workloads/{workload_id}/default",
                    f"default option {workload['default']!r} is not declared for workload {workload_id!r}.",
                )
            )
    return diagnostics


def _runtime_binding_option_errors(
    workload_id: str, option: Mapping[str, Any], resolved: bool
) -> list[RuntimeBindingDiagnostic]:
    components = option["runtime"]["components"]
    component_names = [component["name"] for component in components]
    if len(component_names) == len(set(component_names)):
        return _runtime_observation_errors(workload_id, option) if resolved else []
    diagnostics = [
        RuntimeBindingDiagnostic(
            "RUNTIME_BINDING_COMPONENT_DUPLICATE",
            f"spec/workloads/{workload_id}/options/{option['name']}/runtime/components",
            f"workload option {option['name']!r} has duplicate runtime component names.",
        )
    ]
    if resolved:
        diagnostics.extend(_runtime_observation_errors(workload_id, option))
    return diagnostics


def _runtime_observation_errors(
    workload_id: str, option: Mapping[str, Any]
) -> list[RuntimeBindingDiagnostic]:
    """Keep annotation claims consistent with matching executable probe evidence."""

    runtime = option["runtime"]
    language = runtime["language"]
    version = runtime["version"]
    probe_status = runtime["probeStatus"]
    matching = [
        component for component in runtime["components"] if component["name"].casefold() == language
    ]
    path = f"spec/workloads/{workload_id}/options/{option['name']}/runtime"
    if not matching:
        if probe_status == "matched":
            return [
                RuntimeBindingDiagnostic(
                    "RUNTIME_BINDING_RUNTIME_PROBE_STATUS_MISMATCH",
                    path,
                    "runtime metadata cannot be matched without a matching declared component.",
                )
            ]
        return []

    component = matching[0]
    component_matched = component["probeStatus"] == "matched"
    component_version = component.get("version")
    if component_matched and component_version != version:
        return [
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_RUNTIME_VERSION_MISMATCH",
                path,
                "runtime version annotation differs from the matching executable probe.",
            )
        ]
    if component_matched != (probe_status == "matched"):
        return [
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_RUNTIME_PROBE_STATUS_MISMATCH",
                path,
                "runtime probe status must reflect the matching executable probe.",
            )
        ]
    return []


def _runtime_binding_plugin_link_errors(
    document: Mapping[str, Any],
    descriptor: Mapping[str, Any],
    resolved: bool,
) -> list[RuntimeBindingDiagnostic]:
    """Require the binding to match the exact custom-runner contract closure."""

    errors: list[RuntimeBindingDiagnostic] = []
    try:
        plugin_spec = descriptor["spec"]
        binding_spec = document["spec"]
        binding_workloads = binding_spec["workloads"]
        targets = plugin_spec["targets"]
    except (KeyError, TypeError):
        return [
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_PLUGIN_INVALID",
                "spec/plugin",
                "plugin descriptor is missing required compiled contract fields.",
            )
        ]

    if resolved:
        errors.extend(_resolved_runtime_binding_errors(binding_spec, plugin_spec, targets))

    target_workloads = _custom_runner_workload_ids(targets)
    if set(binding_workloads) != target_workloads:
        errors.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_WORKLOAD_SET_MISMATCH",
                "spec/workloads",
                "binding workloads must exactly match workloads used by the plugin's custom-runner steps.",
            )
        )
    for workload_id in sorted(target_workloads & set(binding_workloads)):
        errors.extend(_workload_contract_errors(workload_id, binding_workloads, targets))
    return errors


def _resolved_runtime_binding_errors(
    binding_spec: Mapping[str, Any],
    plugin_spec: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
) -> list[RuntimeBindingDiagnostic]:
    errors: list[RuntimeBindingDiagnostic] = []
    plugin_reference = binding_spec["plugin"]
    contract = binding_spec["contract"]
    checks = (
        (
            contract.get("pluginRevisionId") != plugin_spec.get("plugin_revision_id"),
            "RUNTIME_BINDING_PLUGIN_REVISION_MISMATCH",
            "spec/contract/pluginRevisionId",
            "binding does not reference this plugin contract revision.",
        ),
        (
            contract.get("contractBundleDigest") != plugin_spec.get("contractBundleDigest"),
            "RUNTIME_BINDING_CONTRACT_BUNDLE_MISMATCH",
            "spec/contract/contractBundleDigest",
            "binding contract bundle digest differs from the plugin descriptor.",
        ),
        (
            contract.get("stepRevisionIds") != _custom_runner_step_revision_ids(targets),
            "RUNTIME_BINDING_STEP_SET_MISMATCH",
            "spec/contract/stepRevisionIds",
            "binding step revision set does not match the plugin's custom-runner steps.",
        ),
    )
    errors.extend(_diagnostics_for_failed_checks(checks))
    if not isinstance(plugin_reference, Mapping) or not isinstance(
        plugin_reference.get("digest"), str
    ):
        errors.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_PLUGIN_REFERENCE_INVALID",
                "spec/plugin",
                "resolved binding must identify the plugin artifact by repository and digest.",
            )
        )
    return errors


def _diagnostics_for_failed_checks(
    checks: Sequence[tuple[bool, str, str, str]],
) -> list[RuntimeBindingDiagnostic]:
    return [
        RuntimeBindingDiagnostic(code, path, message)
        for failed, code, path, message in checks
        if failed
    ]


def _custom_runner_targets(
    targets: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [
        target
        for target in targets
        if target.get("kind") in _STEP_TARGET_KINDS
        and isinstance(target.get("runtime"), Mapping)
        and target["runtime"].get("kind") == "custom-runner"
    ]


def _custom_runner_step_revision_ids(targets: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted(target["step_revision_id"] for target in _custom_runner_targets(targets))


def _custom_runner_workload_ids(targets: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        str(target["runtime"]["workload"])
        for target in _custom_runner_targets(targets)
        if isinstance(target["runtime"].get("workload"), str)
    }


def _workload_contract_errors(
    workload_id: str,
    binding_workloads: Mapping[str, Any],
    targets: Sequence[Mapping[str, Any]],
) -> list[RuntimeBindingDiagnostic]:
    errors: list[RuntimeBindingDiagnostic] = []
    if _workload_needs_entry_point(workload_id, targets):
        errors.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_ENTRY_POINT_MISSING",
                f"spec/workloads/{workload_id}",
                "a custom-runner step assigned to this workload has no entry point.",
            )
        )
    options = binding_workloads[workload_id]["options"]
    if not {option["name"] for option in options}:
        errors.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_OPTIONS_EMPTY",
                f"spec/workloads/{workload_id}/options",
                "every declared workload must have at least one image option.",
            )
        )
    return errors


def _workload_needs_entry_point(workload_id: str, targets: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        target["runtime"].get("workload") == workload_id
        and not isinstance(target["runtime"].get("entryPoint"), str)
        for target in _custom_runner_targets(targets)
    )


def validate_runtime_binding_index(
    bundle: ContractBundle,
    document: Mapping[str, Any],
    *,
    plugin_digest: str,
    now: datetime,
    checkpoint: RuntimeBindingCheckpoint | None = None,
    allow_expired_predecessor: bool = False,
) -> tuple[RuntimeBindingDiagnostic, ...]:
    """Validate an index against plugin identity, expiry, and replay state.

    Signature, repository authorization, OCI subject verification, and
    persistence remain responsibilities of the caller. Set
    ``allow_expired_predecessor`` only when validating the current index as the
    base for publishing a fresh successor. Consumers must use the default,
    which rejects expired indexes.
    """

    schema_errors = validate_document(bundle, "runtime_binding_index", document)
    if schema_errors:
        return tuple(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_INDEX_SCHEMA_INVALID",
                message.partition(":")[0],
                message,
            )
            for message in schema_errors
        )
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    checkpoint = checkpoint or RuntimeBindingCheckpoint()
    metadata = document["metadata"]
    diagnostics: list[RuntimeBindingDiagnostic] = []
    if metadata["pluginDigest"] != plugin_digest:
        diagnostics.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_INDEX_PLUGIN_MISMATCH",
                "metadata/pluginDigest",
                "runtime binding index does not identify the installed plugin artifact.",
            )
        )
    if metadata["generation"] != checkpoint.generation + 1:
        diagnostics.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_INDEX_GENERATION_INVALID",
                "metadata/generation",
                "runtime binding index generation must immediately follow the accepted generation.",
            )
        )
    previous = metadata.get("previousIndexDigest")
    if previous != checkpoint.index_digest:
        diagnostics.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_INDEX_PREDECESSOR_MISMATCH",
                "metadata/previousIndexDigest",
                "runtime binding index does not extend the accepted index digest.",
            )
        )
    issued_at = datetime.fromisoformat(metadata["issuedAt"].replace("Z", _UTC_OFFSET_SUFFIX))
    expires_at = datetime.fromisoformat(metadata["expiresAt"].replace("Z", _UTC_OFFSET_SUFFIX))
    if (
        issued_at > now
        or (expires_at <= now and not allow_expired_predecessor)
        or expires_at <= issued_at
    ):
        diagnostics.append(
            RuntimeBindingDiagnostic(
                "RUNTIME_BINDING_INDEX_TIME_INVALID",
                "metadata",
                "runtime binding index issue and expiry times are invalid for the current time.",
            )
        )
    seen: set[str] = set()
    for position, entry in enumerate(document["spec"]["entries"]):
        digest = entry["bindingDigest"]
        if digest in seen:
            diagnostics.append(
                RuntimeBindingDiagnostic(
                    "RUNTIME_BINDING_INDEX_DUPLICATE_ENTRY",
                    f"spec/entries/{position}/bindingDigest",
                    "runtime binding index contains the same binding digest more than once.",
                )
            )
        seen.add(digest)
    return tuple(sorted(diagnostics))


@dataclass(frozen=True, order=True)
class CatalogDiagnostic:
    """Stable, non-secret reason returned when a catalog candidate is rejected."""

    code: str
    path: str
    message: str


def validate_catalog_index(
    bundle: ContractBundle,
    document: Mapping[str, Any],
    *,
    source_id: str,
    now: datetime,
    checkpoint: CatalogCheckpoint | None = None,
) -> tuple[CatalogDiagnostic, ...]:
    """Validate a catalog index against one source's replay checkpoint.

    This is intentionally a pure control-plane contract check. OCI resolution,
    signature verification, repository-scope checks, and persistence are owned
    by the catalog service that calls it.
    """

    checkpoint = checkpoint or CatalogCheckpoint()
    schema_errors = validate_document(bundle, "catalog_index", document)
    if schema_errors:
        return tuple(
            CatalogDiagnostic(
                code="CATALOG_INDEX_SCHEMA_INVALID",
                path=message.partition(":")[0],
                message=message,
            )
            for message in schema_errors
        )
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    metadata = document["metadata"]
    specification = document["spec"]
    diagnostics: list[CatalogDiagnostic] = []
    if metadata["sourceId"] != source_id:
        diagnostics.append(
            CatalogDiagnostic(
                code="CATALOG_INDEX_SOURCE_MISMATCH",
                path="metadata/sourceId",
                message="Catalog index sourceId does not match the configured source.",
            )
        )
    if specification["contractVersion"] != bundle.version:
        diagnostics.append(
            CatalogDiagnostic(
                code="CATALOG_INDEX_CONTRACT_UNSUPPORTED",
                path="spec/contractVersion",
                message=f"Catalog index contract must be {bundle.version!r}.",
            )
        )

    issued_at = _parse_rfc3339(metadata["issuedAt"])
    expires_at = _parse_rfc3339(metadata["expiresAt"])
    if expires_at <= issued_at:
        diagnostics.append(
            CatalogDiagnostic(
                code="CATALOG_INDEX_TIME_WINDOW_INVALID",
                path="metadata/expiresAt",
                message="Catalog index expiresAt must be later than issuedAt.",
            )
        )
    if expires_at <= now:
        diagnostics.append(
            CatalogDiagnostic(
                code="CATALOG_INDEX_EXPIRED",
                path="metadata/expiresAt",
                message="Catalog index is expired at the supplied reconciliation time.",
            )
        )
    if metadata["generation"] <= checkpoint.generation:
        diagnostics.append(
            CatalogDiagnostic(
                code="CATALOG_INDEX_GENERATION_REPLAY",
                path="metadata/generation",
                message="Catalog index generation must be greater than the accepted generation.",
            )
        )
    if (
        checkpoint.index_digest is not None
        and metadata.get("previousIndexDigest") != checkpoint.index_digest
    ):
        diagnostics.append(
            CatalogDiagnostic(
                code="CATALOG_INDEX_PREDECESSOR_MISMATCH",
                path="metadata/previousIndexDigest",
                message="Catalog index predecessor does not match the accepted index digest.",
            )
        )

    seen_identities: set[tuple[str, str, str]] = set()
    for index, entry in enumerate(specification["entries"]):
        identity = (entry["namespace"], entry["name"], entry["version"])
        if identity in seen_identities:
            diagnostics.append(
                CatalogDiagnostic(
                    code="CATALOG_ENTRY_IDENTITY_DUPLICATE",
                    path=f"spec/entries/{index}",
                    message="Catalog index entries must not repeat namespace, name, and version.",
                )
            )
        seen_identities.add(identity)
    return tuple(sorted(diagnostics))


def _parse_rfc3339(value: str) -> datetime:
    """Parse a value already checked by the bundle's strict date-time format."""

    return datetime.fromisoformat(
        value.removesuffix("Z") + _UTC_OFFSET_SUFFIX if value.endswith("Z") else value
    )
