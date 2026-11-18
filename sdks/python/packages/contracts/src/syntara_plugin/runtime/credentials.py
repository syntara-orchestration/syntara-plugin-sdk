"""Invocation-scoped access to host-approved credential files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import weakref
from _thread import LockType
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any, Literal, Protocol
from urllib.parse import unquote

from syntara_plugin.contracts import canonical_json_bytes

_Reason = Literal["undeclared", "missing", "unauthorized", "unavailable", "expired"]
_NAME = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_MOUNT = re.compile(r"^/var/run/syntara/secrets/[A-Za-z0-9._-]+$")
_FILE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_CREDENTIAL_BYTES = 1_048_576
_MAX_FINGERPRINTS = 256
_MAX_CREDENTIAL_NODES = 10_000
_MAX_OUTPUT_NODES = 10_000
_MAX_SCAN_WORK = 4_000_000
_ROLLING_BASE = 257
_ROLLING_MASK = (1 << 128) - 1
_PROVIDER_SERIALIZATION_ERROR = "Credential providers cannot be serialized"


class _CredentialScanStatus(StrEnum):
    CLEAN = "clean"
    SECRET_FOUND = "secret_found"
    LIMIT_EXCEEDED = "limit_exceeded"


class CredentialAccessError(RuntimeError):
    """A safe credential access failure; no binding name or provider detail is exposed."""

    def __init__(self, reason: _Reason) -> None:
        if reason not in {"undeclared", "missing", "unauthorized", "unavailable", "expired"}:
            reason = "unavailable"
        self.reason = reason
        super().__init__(f"Credential binding {reason}.")


class ScopedCredentialProvider(Protocol):
    """Host-supplied view containing only one invocation's allowed bindings.

    Every successful read must synchronously call the callback supplied to
    ``restrict`` on the same thread and in the calling context before returning.
    ``close`` must revoke later reads and discard the view's bindings.
    """

    def read(self, name: str) -> Mapping[str, Any]:
        """Read one allowed binding or raise a safe ``CredentialAccessError``."""
        ...

    def close(self) -> None:
        """Revoke this view and discard references to its bindings."""
        ...


class CredentialProvider(Protocol):
    """Trusted host provider; it must not log values or include them in errors."""

    def restrict(
        self,
        names: frozenset[str],
        on_read: Callable[[Mapping[str, Any]], None],
    ) -> ScopedCredentialProvider:
        """Return a new view retaining only ``names`` and invoking ``on_read``.

        The view must not retain access to any other binding, even if its internal
        state is inspected. Each successful read must invoke ``on_read``
        synchronously on the same thread and in the calling context before it
        returns. The view must revoke reads on close. A provider that cannot
        supply this view is denied access.
        """
        ...


def approved_credential_path(path: object, local_directory: Path | None = None) -> bool:
    """Apply the container ABI path policy and its default-off local exception."""

    if not isinstance(path, str):
        return False
    if _MOUNT.fullmatch(path):
        return True
    if local_directory is None:
        return False
    candidate = Path(os.path.realpath(path))
    return bool(_FILE.fullmatch(candidate.name)) and candidate.parent == Path(
        os.path.realpath(local_directory)
    )


class FileCredentialProvider:
    """Provider for approved paths. The host owns provisioning and cleanup."""

    __slots__ = ("_paths", "_local_directory")

    def __init__(self, paths: Mapping[str, str], *, local_directory: Path | None = None) -> None:
        if not isinstance(paths, Mapping) or any(
            not isinstance(name, str)
            or not _NAME.fullmatch(name)
            or not approved_credential_path(path, local_directory)
            for name, path in paths.items()
        ):
            raise ValueError("Credential file paths are not approved.")
        self._paths = dict(paths)
        self._local_directory = local_directory

    def __repr__(self) -> str:
        return "FileCredentialProvider(<opaque>)"

    def __reduce__(self) -> Any:
        raise TypeError(_PROVIDER_SERIALIZATION_ERROR)

    def _container_arguments(self) -> tuple[dict[str, str], Path | None]:
        return dict(self._paths), self._local_directory

    def __deepcopy__(self, memo: dict[int, Any]) -> FileCredentialProvider:
        return self

    def read(self, name: str) -> Mapping[str, Any]:
        path = self._paths.get(name)
        if path is None:
            raise CredentialAccessError("missing")
        return _read_credential_file(path, self._local_directory)

    def restrict(
        self,
        names: frozenset[str],
        on_read: Callable[[Mapping[str, Any]], None],
    ) -> ScopedCredentialProvider:
        """Return a detached view with no paths outside the installed allowlist."""
        paths = {name: path for name, path in self._paths.items() if name in names}
        return _ScopedFileCredentialProvider(paths, self._local_directory, on_read)


def _read_credential_file(path: str, local_directory: Path | None) -> dict[str, Any]:
    try:
        if not approved_credential_path(path, local_directory):
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(_MAX_CREDENTIAL_BYTES + 1)
        if len(raw) > _MAX_CREDENTIAL_BYTES:
            raise ValueError
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError
        canonical_json_bytes(document)
    except (OSError, ValueError, TypeError, RecursionError):
        raise CredentialAccessError("unavailable") from None
    return document


class _ScopedFileCredentialProvider:
    """A revocable file view whose retained paths are already restricted."""

    __slots__ = ("__paths", "__local_directory", "__on_read", "__active")

    def __init__(
        self,
        paths: Mapping[str, str],
        local_directory: Path | None,
        on_read: Callable[[Mapping[str, Any]], None],
    ) -> None:
        self.__paths = dict(paths)
        self.__local_directory = local_directory
        self.__on_read: Callable[[Mapping[str, Any]], None] | None = on_read
        self.__active = True

    def __repr__(self) -> str:
        return "ScopedFileCredentialProvider(<opaque>)"

    def __deepcopy__(self, memo: dict[int, Any]) -> _ScopedFileCredentialProvider:
        return self

    def __reduce__(self) -> Any:
        raise TypeError(_PROVIDER_SERIALIZATION_ERROR)

    def read(self, name: str) -> Mapping[str, Any]:
        if not self.__active:
            raise CredentialAccessError("expired")
        if not isinstance(name, str) or name not in self.__paths:
            raise CredentialAccessError("missing")
        document = _read_credential_file(self.__paths[name], self.__local_directory)
        on_read = self.__on_read
        if on_read is None:
            raise CredentialAccessError("expired")
        try:
            on_read(document)
        except Exception:
            raise CredentialAccessError("unavailable") from None
        return document

    def close(self) -> None:
        self.__active = False
        self.__paths.clear()
        self.__on_read = None


def _rolling_hash(value: str) -> int:
    """Compute a rolling hash over Unicode code points without retaining text."""
    result = 0
    for character in value:
        result = (result * _ROLLING_BASE + ord(character) + 1) & _ROLLING_MASK
    return result


def _rolling_digest(value: int) -> bytes:
    # Hash the rolling value before retaining it. Short values must not be
    # recoverable by simply decoding the integer fingerprint.
    return hashlib.sha256(value.to_bytes(16, "big")).digest()


def _scalar_fingerprint(value: Any) -> bytes | None:
    if value is not None and isinstance(value, (bool, int, float)):
        return hashlib.sha256(canonical_json_bytes(value)).digest()
    return None


def _enqueue_children(value: Any, pending: list[Any], remaining: int) -> bool | None:
    """Queue container members; return true if the traversal budget is exceeded."""

    if isinstance(value, Mapping):
        if 2 * len(value) > remaining - len(pending):
            return True
        pending.extend(value.keys())
        pending.extend(value.values())
        return False
    if isinstance(value, (list, tuple)):
        if len(value) > remaining - len(pending):
            return True
        pending.extend(value)
        return False
    return None


def _follow_local_pointer_part(current: Any, part: str, root: Any) -> Any:
    """Resolve one validated JSON Pointer part within the root resource."""

    if current is not root and isinstance(current, Mapping) and "$id" in current:
        return None
    if re.search(r"~(?![01])", part):
        return None
    part = part.replace("~1", "/").replace("~0", "~")
    if isinstance(current, Mapping):
        return current[part]
    if isinstance(current, (list, tuple)) and re.fullmatch(r"0|[1-9]\d*", part, flags=re.ASCII):
        return current[int(part)]
    return None


def _local_schema_reference(root: Any, reference: Any) -> Any:
    """Resolve a local JSON Pointer without fetching external schemas."""

    if not isinstance(reference, str) or not reference.startswith("#"):
        return None
    if re.search(r"%(?![\da-fA-F]{2})", reference, flags=re.ASCII):
        return None
    try:
        pointer = unquote(reference[1:], errors="strict")
        if not pointer:
            return root
        if not pointer.startswith("/"):
            return None
        current = root
        for part in pointer[1:].split("/"):
            current = _follow_local_pointer_part(current, part, root)
            if current is None:
                return None
        return current
    except (KeyError, IndexError, ValueError):
        return None


def _literal_pattern_matches(pattern: Any, key: str, budget: list[int]) -> bool | None:
    """Match the supported literal subset of JSON Schema property patterns."""

    if not isinstance(pattern, str):
        return None
    budget[0] -= 1 + len(pattern) + len(key)
    if budget[0] < 0 or not re.fullmatch(r"\^?[A-Za-z0-9_-]+\$?", pattern):
        return None
    starts = pattern.startswith("^")
    ends = pattern.endswith("$")
    literal = pattern[int(starts):len(pattern) - int(ends)]
    if starts and ends:
        return key == literal
    if starts:
        return key.startswith(literal)
    if ends:
        return key.endswith(literal)
    return literal in key


def _additional_properties_apply(schema: Mapping[str, Any], key: Any, budget: list[int]) -> bool:
    """Allow additional properties only when no supported pattern matches."""

    patterns = schema.get("patternProperties", {})
    if not isinstance(patterns, Mapping) or not isinstance(key, str):
        return False
    for pattern in patterns:
        matches = _literal_pattern_matches(pattern, key, budget)
        if matches is None or matches:
            return False
    return True


def _direct_child_declarations(
    schema: Mapping[str, Any], key: Any, budget: list[int], include_additional: bool
) -> list[Any]:
    """Collect declarations written directly on a schema object."""

    declarations: list[Any] = []
    if key is None:
        if "items" in schema and isinstance(schema["items"], (Mapping, bool)):
            declarations.append(schema["items"])
    else:
        properties = schema.get("properties")
        if isinstance(properties, Mapping) and key in properties:
            declarations.append(properties[key])
        elif include_additional:
            additional = schema.get("additionalProperties")
            if isinstance(additional, (Mapping, bool)) and _additional_properties_apply(
                schema, key, budget
            ):
                declarations.append(additional)
    return declarations


def _referenced_child_declaration(
    schema: Mapping[str, Any], root: Any, key: Any, budget: list[int],
    ancestors: frozenset[int], include_additional: bool,
) -> Any:
    """Resolve and inspect a local reference, if one is declared."""

    if "$ref" not in schema:
        return None
    reference = schema["$ref"]
    if isinstance(reference, str) and reference.startswith("#"):
        budget[0] -= 1 + len(reference)
        if budget[0] < 0:
            return None
    target = _local_schema_reference(root, reference)
    return _declared_child_schema(
        target, root, key, budget, ancestors, include_additional=include_additional
    )


def _all_of_child_declarations(
    schema: Mapping[str, Any], root: Any, key: Any, budget: list[int],
    ancestors: frozenset[int], include_additional: bool,
) -> list[Any]:
    """Collect every available declaration from an allOf."""

    branches = schema.get("allOf")
    if not isinstance(branches, (list, tuple)):
        return []
    declarations: list[Any] = []
    for branch in branches:
        child = _declared_child_schema(
            branch, root, key, budget, ancestors, include_additional=include_additional
        )
        if child is not None:
            declarations.append(child)
    return declarations


def _alternative_child_declaration(
    schema: Mapping[str, Any], keyword: str, root: Any, key: Any, budget: list[int],
    ancestors: frozenset[int], include_additional: bool,
) -> Any:
    """Return an alternative only when every branch declares the child."""

    branches = schema.get(keyword)
    if not isinstance(branches, (list, tuple)) or not branches:
        return None
    children = [
        _declared_child_schema(
            branch, root, key, budget, ancestors, include_additional=include_additional
        )
        for branch in branches
    ]
    if all(child is not None for child in children):
        return {keyword: children}
    return None


def _declared_child_schema(
    schema: Any, root: Any, key: Any, budget: list[int], ancestors: frozenset[int] = frozenset(),
    *, include_additional: bool = False,
) -> Any:
    """Find a child's declared schema, conservatively intersecting alternatives."""

    budget[0] -= 1
    if budget[0] < 0 or len(ancestors) >= 80:
        budget[0] = -1
        return None
    if not isinstance(schema, Mapping) or id(schema) in ancestors:
        return None
    # Nested identifiers change reference resolution. Leave those resources opaque.
    if schema is not root and "$id" in schema:
        return None
    ancestors = ancestors | {id(schema)}
    declarations = _direct_child_declarations(schema, key, budget, include_additional)
    referenced = _referenced_child_declaration(
        schema, root, key, budget, ancestors, include_additional
    )
    if referenced is not None:
        declarations.append(referenced)
    declarations.extend(
        _all_of_child_declarations(
            schema, root, key, budget, ancestors, include_additional
        )
    )
    for keyword in ("anyOf", "oneOf"):
        alternative = _alternative_child_declaration(
            schema, keyword, root, key, budget, ancestors, include_additional
        )
        if alternative is not None:
            declarations.append(alternative)
    if not declarations:
        return None
    if len(declarations) == 1:
        return declarations[0]
    return {"allOf": declarations}


class _ScopedCredentialProvider:
    """Restrict provider reads and track every successful read in the scope."""

    __slots__ = ("__allowed", "__provider", "__record", "__active", "__read_name", "__lock")

    def __init__(
        self,
        allowed: frozenset[str],
        provider: ScopedCredentialProvider,
        record: Callable[[Mapping[str, Any]], None],
        read_name: ContextVar[str | None],
        lock: LockType,
    ) -> None:
        self.__allowed = allowed
        self.__provider: ScopedCredentialProvider | None = provider
        self.__record: Callable[[Mapping[str, Any]], None] | None = record
        self.__active = True
        self.__read_name = read_name
        self.__lock = lock

    def __repr__(self) -> str:
        return "ScopedCredentialProvider(<opaque>)"

    def __deepcopy__(self, memo: dict[int, Any]) -> _ScopedCredentialProvider:
        return self

    def __reduce__(self) -> Any:
        raise TypeError(_PROVIDER_SERIALIZATION_ERROR)

    def read(self, name: str) -> dict[str, Any]:
        with self.__lock:
            if not self.__active:
                raise CredentialAccessError("expired")
            if not isinstance(name, str) or name not in self.__allowed:
                raise CredentialAccessError("undeclared")
            provider = self.__provider
            record = self.__record
            if provider is None or record is None:
                raise CredentialAccessError("expired")
        # Providers call on_read before exposing the document. Attribute only
        # callbacks in this read's context; any other read keeps every key secret.
        token = self.__read_name.set(name)
        try:
            value = provider.read(name)
            if not isinstance(value, Mapping):
                raise ValueError
            encoded = canonical_json_bytes(dict(value))
            if len(encoded) > _MAX_CREDENTIAL_BYTES:
                raise ValueError
            document: dict[str, Any] = json.loads(encoded)
        except CredentialAccessError as error:
            raise CredentialAccessError(error.reason) from None
        except Exception:
            raise CredentialAccessError("unavailable") from None
        finally:
            self.__read_name.reset(token)
        with self.__lock:
            if not self.__active:
                raise CredentialAccessError("expired")
            return document

    def _revoke(self) -> None:
        """Revoke the view while the scope's shared lock is held."""
        self.__active = False

    def close(self) -> None:
        with self.__lock:
            self._revoke()
            provider = self.__provider
            self.__provider = None
            self.__record = None
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass


class _CredentialScope:
    """A per-invocation capability, containing fingerprints but no raw values."""

    __slots__ = (
        "_allowed", "_provider", "_active", "_strings", "_scalars",
        "_fingerprint_count", "_overflow", "_has_read", "_restriction_failed", "__weakref__",
        "_schemas", "_read_name", "_record_lock",
    )

    def __init__(
        self, allowed: tuple[str, ...], provider: CredentialProvider | None,
        schemas: Mapping[str, Any] | None = None,
    ) -> None:
        self._allowed = frozenset(allowed)
        self._active = True
        self._strings: dict[int, set[bytes]] = {}
        self._scalars: set[bytes] = set()
        self._fingerprint_count = 0
        self._overflow = False
        self._has_read = False
        self._restriction_failed = False
        self._schemas = schemas or {}
        self._read_name: ContextVar[str | None] = ContextVar("credential_read_name", default=None)
        self._record_lock = Lock()
        self._provider: _ScopedCredentialProvider | None = None
        if provider is not None:
            scope_ref = weakref.ref(self)

            def record(document: Mapping[str, Any]) -> None:
                scope = scope_ref()
                if scope is None:
                    raise CredentialAccessError("expired")
                scope._record(document)

            try:
                restricted = provider.restrict(self._allowed, record)
                if (
                    restricted is provider
                    or not callable(getattr(restricted, "read", None))
                    or not callable(getattr(restricted, "close", None))
                ):
                    raise ValueError
            except Exception:
                self._restriction_failed = True
            else:
                self._provider = _ScopedCredentialProvider(
                    self._allowed, restricted, self._record, self._read_name, self._record_lock
                )

    def __repr__(self) -> str:
        return "CredentialScope(<opaque>)"

    def __deepcopy__(self, memo: dict[int, Any]) -> _CredentialScope:
        return self

    def _authorize(self, name: str) -> None:
        if not self._active:
            raise CredentialAccessError("expired")
        if not isinstance(name, str) or name not in self._allowed:
            raise CredentialAccessError("undeclared")

    def handle(self, name: str) -> CredentialBinding:
        self._authorize(name)
        if self._provider is None:
            raise CredentialAccessError("unauthorized" if self._restriction_failed else "missing")
        return CredentialBinding(self, name)

    def read(self, name: str) -> dict[str, Any]:
        with self._record_lock:
            self._authorize(name)
            provider = self._provider
            if provider is None:
                raise CredentialAccessError("unauthorized" if self._restriction_failed else "missing")
        return provider.read(name)

    def _add_text(self, value: str) -> None:
        if not value or self._overflow:
            return
        digest = _rolling_digest(_rolling_hash(value))
        fingerprints = self._strings.setdefault(len(value), set())
        if digest not in fingerprints:
            fingerprints.add(digest)
            self._fingerprint_count += 1
            if self._fingerprint_count > _MAX_FINGERPRINTS:
                self._overflow = True

    def _record(self, document: Mapping[str, Any]) -> None:
        with self._record_lock:
            if not self._active:
                raise CredentialAccessError("expired")
            self._record_document(document, self._schemas.get(self._read_name.get()))

    def _enqueue_mapping_children(
        self, value: Mapping[Any, Any], current_schema: Any, root_schema: Any,
        pending: list[tuple[Any, Any]], schema_budget: list[int], nodes: int,
    ) -> int | None:
        """Queue object values and any opaque keys within the node budget."""

        if 2 * len(value) > _MAX_CREDENTIAL_NODES - nodes - len(pending):
            return None
        added_nodes = 0
        for key, child in value.items():
            declaration = _declared_child_schema(
                current_schema, root_schema, key, schema_budget
            )
            child_schema = _declared_child_schema(
                current_schema, root_schema, key, schema_budget, include_additional=True
            )
            if schema_budget[0] < 0:
                return None
            if declaration is None:
                pending.append((key, None))
            else:
                # Public keys still count towards the traversal budget.
                added_nodes += 1
            pending.append((child, child_schema))
        return added_nodes

    def _enqueue_sequence_children(
        self, value: list[Any] | tuple[Any, ...], current_schema: Any,
        root_schema: Any, pending: list[tuple[Any, Any]], schema_budget: list[int],
        nodes: int,
    ) -> bool:
        """Queue array items and their schemas within the node budget."""

        if len(value) > _MAX_CREDENTIAL_NODES - nodes - len(pending):
            return False
        items_schema = _declared_child_schema(current_schema, root_schema, None, schema_budget)
        if schema_budget[0] < 0:
            return False
        pending.extend((child, items_schema) for child in value)
        return True

    def _record_document(self, document: Mapping[str, Any], schema: Any) -> None:
        self._has_read = True
        pending: list[tuple[Any, Any]] = [(document, schema)]
        nodes = 0
        schema_budget = [_MAX_CREDENTIAL_NODES]
        while pending and not self._overflow:
            value, current_schema = pending.pop()
            nodes += 1
            if nodes > _MAX_CREDENTIAL_NODES:
                self._overflow = True
                break
            if isinstance(value, Mapping):
                added_nodes = self._enqueue_mapping_children(
                    value, current_schema, schema, pending, schema_budget, nodes
                )
                if added_nodes is None:
                    self._overflow = True
                    break
                nodes += added_nodes
            elif isinstance(value, (list, tuple)):
                if not self._enqueue_sequence_children(
                    value, current_schema, schema, pending, schema_budget, nodes
                ):
                    self._overflow = True
                    break
            else:
                self._record_leaf(value)

    def _record_leaf(self, value: Any) -> None:
        if isinstance(value, str):
            self._add_text(value)
            return
        fingerprint = _scalar_fingerprint(value)
        if fingerprint is None:
            return
        if fingerprint not in self._scalars:
            self._scalars.add(fingerprint)
            self._fingerprint_count += 1
            if self._fingerprint_count > _MAX_FINGERPRINTS:
                self._overflow = True
        # Python string conversion and JSON conversion differ for bools.
        self._add_text(str(value))
        self._add_text(canonical_json_bytes(value).decode("utf-8"))

    @property
    def has_read(self) -> bool:
        return self._has_read

    def contains_secret(self, value: Any) -> bool:
        """Check wrapper data with bounded work; uncertainty fails closed."""
        return self.scan(value) is not _CredentialScanStatus.CLEAN

    def scan(self, value: Any) -> _CredentialScanStatus:
        """Distinguish clean data, a detected secret, and an incomplete scan."""
        with self._record_lock:
            if not self._active:
                return _CredentialScanStatus.LIMIT_EXCEEDED
            return self._scan_active(value)

    def _scan_active(self, value: Any) -> _CredentialScanStatus:
        if self._overflow:
            return _CredentialScanStatus.LIMIT_EXCEEDED
        if not self._strings and not self._scalars:
            return _CredentialScanStatus.CLEAN
        budget = [_MAX_SCAN_WORK]
        pending = [value]
        nodes = 0
        while pending:
            current = pending.pop()
            nodes += 1
            if nodes > _MAX_OUTPUT_NODES:
                return _CredentialScanStatus.LIMIT_EXCEEDED
            queued = _enqueue_children(current, pending, _MAX_OUTPUT_NODES - nodes)
            if queued is True:
                return _CredentialScanStatus.LIMIT_EXCEEDED
            if queued is None:
                status = self._scan_leaf(current, budget)
                if status is not _CredentialScanStatus.CLEAN:
                    return status
        return _CredentialScanStatus.CLEAN

    def _scan_leaf(self, value: Any, budget: list[int]) -> _CredentialScanStatus:
        if isinstance(value, str):
            return self._scan_text(value, budget)
        fingerprint = _scalar_fingerprint(value)
        if fingerprint is not None and fingerprint in self._scalars:
            return _CredentialScanStatus.SECRET_FOUND
        if fingerprint is None:
            return _CredentialScanStatus.CLEAN
        text = str(value)
        status = self._scan_text(text, budget)
        if status is not _CredentialScanStatus.CLEAN:
            return status
        json_text = canonical_json_bytes(value).decode("utf-8")
        return (
            _CredentialScanStatus.CLEAN
            if text == json_text
            else self._scan_text(json_text, budget)
        )

    def _scan_text(self, value: str, budget: list[int]) -> _CredentialScanStatus:
        for length, fingerprints in self._strings.items():
            if length > len(value):
                continue
            windows = len(value) - length + 1
            # Initial window construction and every following hash are linear
            # in the output length, independent of the credential length.
            work = length + windows
            if work > budget[0]:
                return _CredentialScanStatus.LIMIT_EXCEEDED
            budget[0] -= work
            rolling = _rolling_hash(value[:length])
            if _rolling_digest(rolling) in fingerprints:
                return _CredentialScanStatus.SECRET_FOUND
            power = pow(_ROLLING_BASE, length - 1, 1 << 128)
            for index in range(length, len(value)):
                rolling = (
                    (rolling - (ord(value[index - length]) + 1) * power)
                    * _ROLLING_BASE + ord(value[index]) + 1
                ) & _ROLLING_MASK
                if _rolling_digest(rolling) in fingerprints:
                    return _CredentialScanStatus.SECRET_FOUND
        return _CredentialScanStatus.CLEAN

    def close(self) -> None:
        with self._record_lock:
            self._active = False
            provider = self._provider
            self._provider = None
            if provider is not None:
                provider._revoke()
            self._strings.clear()
            self._scalars.clear()
            self._schemas = {}
        # The provider may wait for a callback; do not hold the record lock.
        if provider is not None:
            provider.close()


class CredentialBinding:
    """Opaque, invocation-scoped handle. Read only while the step executes."""

    __slots__ = ("__scope", "__name")

    def __init__(self, scope: _CredentialScope, name: str) -> None:
        self.__scope = scope
        self.__name = name

    def __repr__(self) -> str:
        return "CredentialBinding(<opaque>)"

    def __reduce__(self) -> Any:
        raise TypeError("Credential bindings cannot be serialized")

    def read(self) -> dict[str, Any]:
        """Read a detached credential document; callers must keep it private."""
        return self.__scope.read(self.__name)
