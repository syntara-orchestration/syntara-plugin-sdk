"""Invocation-scoped, best-effort operational logging."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event, RLock
from typing import TYPE_CHECKING, Any
from weakref import finalize

if TYPE_CHECKING:
    from .protocol import container_pb2

_LEVELS = frozenset({"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"})
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_MAX_BATCH_BYTES = 262_144
_MAX_EVENTS = 128
_MAX_DROPPED = 4_294_967_295


def _snapshot_json(value: Any) -> tuple[bool, Any]:
    """Copy validated JSON while accounting for its compact UTF-8 size.

    The bounded copy is the object subsequently serialized, so caller-owned
    containers cannot change it after validation.
    """
    remaining = [16_384]
    nodes = [2048]

    def spend(size: int) -> bool:
        remaining[0] -= size
        return remaining[0] >= 0

    def string(value: str) -> bool:
        # Even before escaping or UTF-8 encoding, the JSON string needs quotes.
        if len(value) + 2 > remaining[0]:
            return False
        return spend(len(json.dumps(value, ensure_ascii=False).encode("utf-8")))

    def copy(item: Any, depth: int) -> tuple[bool, Any]:
        nodes[0] -= 1
        if nodes[0] < 0:
            return False, None
        if item is None:
            return spend(4), None
        if type(item) is bool:
            return spend(4 if item else 5), item
        if type(item) is str:
            return string(item), item
        if type(item) is int:
            return -(2**63) <= item < 2**63 and spend(len(str(item))), item
        if type(item) is float:
            return math.isfinite(item) and spend(len(json.dumps(item, allow_nan=False))), item
        if depth >= 8:
            return False, None
        if type(item) is list:
            if not spend(2):
                return False, None
            snapshot_list: list[Any] = []
            for index, child in enumerate(item):
                if index and not spend(1):
                    return False, None
                valid, snapshot_child = copy(child, depth + 1)
                if not valid:
                    return False, None
                snapshot_list.append(snapshot_child)
            return True, snapshot_list
        if type(item) is dict:
            if not spend(2):
                return False, None
            snapshot_dict: dict[str, Any] = {}
            for index, (key, child) in enumerate(item.items()):
                if (
                    type(key) is not str
                    or (index and not spend(1))
                    or not string(key)
                    or not spend(1)
                ):
                    return False, None
                valid, snapshot_child = copy(child, depth + 1)
                if not valid:
                    return False, None
                snapshot_dict[key] = snapshot_child
            return True, snapshot_dict
        return False, None

    return copy(value, 0)


def _json_value(value: Any) -> bool:
    return _snapshot_json(value)[0]


@dataclass(frozen=True)
class _LogRecord:
    timestamp: str
    severity: str
    message: str
    event_name: str
    attributes_json: bytes

    def wire_budget(self) -> int:
        # Protobuf field tags, lengths and repeated-message framing fit in 32 bytes.
        return (
            32
            + sum(
                len(value.encode("utf-8"))
                for value in (self.timestamp, self.severity, self.message, self.event_name)
            )
            + len(self.attributes_json)
        )


class ContainerToolbox:
    """Invocation-scoped operations available to a step."""

    __slots__ = ("__weakref__",)

    def log(
        self,
        level: str,
        message: str,
        attributes: dict[str, Any] | None = None,
        *,
        event_name: str | None = None,
    ) -> None:
        """Record an operational event; invalid and excess records are dropped."""
        state = _toolbox_state(self)
        with state.lock:
            if state.closed.is_set() or state.cancellation.is_set():
                return
            try:
                attrs = {} if attributes is None else attributes
                if (
                    type(level) is not str
                    or level not in _LEVELS
                    or type(message) is not str
                    or len(message) > 4096
                    or not 1 <= len(message.encode("utf-8")) <= 4096
                    or (
                        event_name is not None
                        and (type(event_name) is not str or not _NAME.fullmatch(event_name))
                    )
                    or type(attrs) is not dict
                    or len(attrs) > 32
                ):
                    raise ValueError
                valid, snapshot = _snapshot_json(attrs)
                if not valid or len(snapshot) > 32:
                    raise ValueError
                payload = json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                if len(payload) > 16_384:
                    raise ValueError
                event = _LogRecord(
                    timestamp=datetime.now(UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    severity=level,
                    message=message,
                    event_name=event_name or "",
                    attributes_json=payload,
                )
                if (
                    len(state.events) >= _MAX_EVENTS
                    or state.wire_bytes + event.wire_budget() > _MAX_BATCH_BYTES
                ):
                    raise ValueError
                state.wire_bytes += event.wire_budget()
                state.events.append(event)
            except Exception:
                state.dropped = min(_MAX_DROPPED, state.dropped + 1)


class _ToolboxState:
    def __init__(self, cancellation: Event, invocation_id: str) -> None:
        self.cancellation = cancellation
        self.invocation_id = invocation_id
        self.closed = Event()
        self.lock = RLock()
        self.events: list[_LogRecord] = []
        self.wire_bytes = len(invocation_id.encode("utf-8")) + 32
        self.dropped = 0
        self.sent = False


_states: dict[int, _ToolboxState] = {}
_states_lock = RLock()


def _new_toolbox(cancellation: Event, invocation_id: str = "") -> ContainerToolbox:
    toolbox = ContainerToolbox()
    with _states_lock:
        _states[id(toolbox)] = _ToolboxState(cancellation, invocation_id)
    finalize(toolbox, _discard_toolbox_state, id(toolbox))
    return toolbox


def _toolbox_state(toolbox: ContainerToolbox) -> _ToolboxState:
    with _states_lock:
        return _states[id(toolbox)]


def _discard_toolbox_state(toolbox_id: int) -> None:
    with _states_lock:
        _states.pop(toolbox_id, None)


def _toolbox_active(toolbox: ContainerToolbox) -> bool:
    state = _toolbox_state(toolbox)
    return not state.closed.is_set() and not state.cancellation.is_set()


def _close_toolbox(toolbox: ContainerToolbox) -> None:
    state = _toolbox_state(toolbox)
    with state.lock:
        state.closed.set()


def _take_log_batch(toolbox: ContainerToolbox, invocation_id: str) -> container_pb2.LogBatch | None:
    from .protocol import container_pb2

    state = _toolbox_state(toolbox)
    with state.lock:
        state.closed.set()
        if state.sent or not (state.events or state.dropped):
            return None
        state.sent = True
        return container_pb2.LogBatch(
            invocation_id=invocation_id,
            version=1,
            events=[
                container_pb2.LogEvent(
                    timestamp=event.timestamp,
                    severity=event.severity,
                    message=event.message,
                    event_name=event.event_name,
                    attributes_json=event.attributes_json,
                )
                for event in state.events
            ],
            dropped_count=state.dropped,
        )
