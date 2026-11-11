"""Public host-service surface for step execution contexts."""

from __future__ import annotations

from threading import Event, RLock
from weakref import finalize


class ContainerToolbox:
    """Invocation-scoped host-service surface, currently without operations.

    Future capabilities require a typed service contract and a host implementation.
    """

    __slots__ = ("__weakref__",)


class _ToolboxState:
    def __init__(self, cancellation: Event) -> None:
        self.cancellation = cancellation
        self.closed = Event()


# Do not attach transports or mutable lifecycle state to the object given to a step.
_states: dict[int, _ToolboxState] = {}
_states_lock = RLock()


def _new_toolbox(cancellation: Event) -> ContainerToolbox:
    toolbox = ContainerToolbox()
    with _states_lock:
        _states[id(toolbox)] = _ToolboxState(cancellation)
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
    _toolbox_state(toolbox).closed.set()
