"""Invocation-scoped access to host-approved credential files."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

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

    Every successful read must call the callback supplied to ``restrict`` before
    returning. ``close`` must revoke later reads and discard the view's bindings.
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
        state is inspected. Calls through the view must be recorded and revoked
        on close. A provider that cannot supply this view is denied access.
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


class _ScopedCredentialProvider:
    """Restrict provider reads and track every successful read in the scope."""

    __slots__ = ("__allowed", "__provider", "__record", "__active")

    def __init__(
        self,
        allowed: frozenset[str],
        provider: ScopedCredentialProvider,
        record: Callable[[Mapping[str, Any]], None],
    ) -> None:
        self.__allowed = allowed
        self.__provider: ScopedCredentialProvider | None = provider
        self.__record: Callable[[Mapping[str, Any]], None] | None = record
        self.__active = True

    def __repr__(self) -> str:
        return "ScopedCredentialProvider(<opaque>)"

    def __deepcopy__(self, memo: dict[int, Any]) -> _ScopedCredentialProvider:
        return self

    def __reduce__(self) -> Any:
        raise TypeError(_PROVIDER_SERIALIZATION_ERROR)

    def read(self, name: str) -> dict[str, Any]:
        if not self.__active:
            raise CredentialAccessError("expired")
        if not isinstance(name, str) or name not in self.__allowed:
            raise CredentialAccessError("undeclared")
        provider = self.__provider
        record = self.__record
        if provider is None or record is None:
            raise CredentialAccessError("expired")
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
        return document

    def close(self) -> None:
        self.__active = False
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
        "_fingerprint_count", "_overflow", "_has_read", "_restriction_failed",
    )

    def __init__(self, allowed: tuple[str, ...], provider: CredentialProvider | None) -> None:
        self._allowed = frozenset(allowed)
        self._active = True
        self._strings: dict[int, set[bytes]] = {}
        self._scalars: set[bytes] = set()
        self._fingerprint_count = 0
        self._overflow = False
        self._has_read = False
        self._restriction_failed = False
        self._provider: _ScopedCredentialProvider | None = None
        if provider is not None:
            try:
                restricted = provider.restrict(self._allowed, self._record)
                if (
                    restricted is provider
                    or not callable(getattr(restricted, "read", None))
                    or not callable(getattr(restricted, "close", None))
                ):
                    raise ValueError
            except Exception:
                self._restriction_failed = True
            else:
                self._provider = _ScopedCredentialProvider(self._allowed, restricted, self._record)

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
        self._authorize(name)
        if self._provider is None:
            raise CredentialAccessError("unauthorized" if self._restriction_failed else "missing")
        return self._provider.read(name)

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
        self._has_read = True
        pending: list[Any] = [document]
        nodes = 0
        while pending and not self._overflow:
            value = pending.pop()
            nodes += 1
            if nodes > _MAX_CREDENTIAL_NODES:
                self._overflow = True
                break
            queued = _enqueue_children(value, pending, _MAX_CREDENTIAL_NODES - nodes)
            if queued is True:
                self._overflow = True
                break
            if queued is None:
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
        self._active = False
        if self._provider is not None:
            self._provider.close()
        self._provider = None
        self._strings.clear()
        self._scalars.clear()


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
