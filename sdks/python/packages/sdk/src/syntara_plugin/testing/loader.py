"""Entry-point loading for locally executable plugin steps."""

from __future__ import annotations

import importlib
import re
from typing import Any

from syntara_plugin.runtime import BaseStep

from .errors import HarnessUsageError

_ENTRY_POINT = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):([A-Za-z_][A-Za-z0-9_]*)$")
_IDENTITY_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]")

StepTarget = str | type[BaseStep[Any, Any]] | BaseStep[Any, Any]


def default_step_identity(step: BaseStep[Any, Any]) -> str:
    """Return a stable fallback identity derived from one step's class.

    Classes defined inside functions carry ``<locals>`` qualifiers, which are
    not valid identity characters; those characters are replaced with dashes
    so locally defined test steps remain bindable.
    """

    step_class = type(step)
    raw = f"{step_class.__module__}.{step_class.__qualname__}"
    return _IDENTITY_UNSAFE.sub("-", raw)


def parse_entry_point(entry_point: str) -> tuple[str, str]:
    """Split one ``module:Class`` entry point into its module and class names."""

    match = _ENTRY_POINT.fullmatch(entry_point)
    if match is None:
        message = (
            f"entry point {entry_point!r} must use the 'module:Class' form, for example "
            "'my_plugin.steps.notify:NotifyStep'"
        )
        raise HarnessUsageError(message)
    return match.group(1), match.group(2)


def entry_point_of(step_class: type[BaseStep[Any, Any]]) -> str:
    """Return the ``module:Class`` entry point string for one step class."""

    return f"{step_class.__module__}:{step_class.__qualname__}"


def load_step_class(entry_point: str) -> type[BaseStep[Any, Any]]:
    """Import the ``BaseStep`` subclass named by one ``module:Class`` entry point."""

    module_name, class_name = parse_entry_point(entry_point)
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        message = f"entry point {entry_point!r} could not import module {module_name!r}: {error}"
        raise HarnessUsageError(message) from error
    step_class = getattr(module, class_name, None)
    if step_class is None:
        message = f"entry point {entry_point!r}: module {module_name!r} has no class {class_name!r}"
        raise HarnessUsageError(message)
    if not (isinstance(step_class, type) and issubclass(step_class, BaseStep)):
        message = f"entry point {entry_point!r} must name a BaseStep subclass"
        raise HarnessUsageError(message)
    return step_class


def resolve_step(target: StepTarget) -> BaseStep[Any, Any]:
    """Resolve an entry point, step class, or step instance into one instance."""

    step: Any = target
    if isinstance(target, str):
        step = load_step_class(target)
    if isinstance(step, type):
        try:
            step = step()
        except TypeError as error:
            message = f"{step.__qualname__} must be constructible without arguments"
            raise HarnessUsageError(message) from error
    if not isinstance(step, BaseStep):
        message = "expected an entry point, a BaseStep class or instance, or a StepBinding"
        raise HarnessUsageError(message)
    return step
