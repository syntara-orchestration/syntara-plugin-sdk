"""Typed Python execution API for Syntara plugin steps."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TypeVar, get_args, get_origin

from pydantic import BaseModel

from .context import ExecutionContext


def _resolve_models(cls: type[BaseNode[Any, Any]]) -> tuple[type[BaseModel], type[BaseModel]]:
    """Resolve the concrete Pydantic models from a class's generic substitutions.

    Supported authoring pattern::

        class MyStep(ActionStep[MyInput, MyOutput]):
            ...

    The helper walks the MRO from base to derived class while tracking how each
    generic level specializes ``BaseNode``'s type variables, so taxonomy
    subclasses can stay generic without forcing constructor boilerplate.
    """

    substitutions: dict[TypeVar, Any] = {}
    node_arguments: tuple[Any, ...] | None = None

    def resolve(argument: Any) -> Any:
        return substitutions.get(argument, argument) if isinstance(argument, TypeVar) else argument

    for klass in reversed(cls.__mro__):
        for base in getattr(klass, "__orig_bases__", ()):
            origin = get_origin(base)
            if not isinstance(origin, type) or not issubclass(origin, BaseNode):
                continue
            parameters = origin.__parameters__
            arguments = get_args(base)
            if len(parameters) != len(arguments):
                continue
            resolved_arguments = tuple(resolve(argument) for argument in arguments)
            substitutions.update(zip(parameters, resolved_arguments, strict=True))
            if origin is BaseNode:
                node_arguments = arguments

    if node_arguments is None:
        message = f"{cls.__qualname__} does not specialize BaseNode[TInput, TOutput]"
        raise TypeError(message)

    resolved_models = tuple(resolve(argument) for argument in node_arguments)
    for model in resolved_models:
        if isinstance(model, TypeVar) or not isinstance(model, type) or not issubclass(model, BaseModel):
            message = f"{cls.__qualname__} must specialize BaseNode with concrete Pydantic v2 models"
            raise TypeError(message)

    input_model, output_model = resolved_models
    return input_model, output_model


class BaseNode[TInput: BaseModel, TOutput: BaseModel](ABC):
    """Abstract typed base class shared by every executable plugin step.

    ``execute()`` is the stable author-facing entrypoint. It accepts an
    already-constructed ``TInput`` and returns the step's ``TOutput`` directly;
    raw-input validation and the common output envelope are added at the
    dispatch boundary by their own stories. Subclasses implement ``run()`` and
    that hook must remain stable across those additions.
    """

    @classmethod
    def input_model(cls) -> type[BaseModel]:
        """Return the concrete Pydantic input model for this step class."""

        return _resolve_models(cls)[0]

    @classmethod
    def output_model(cls) -> type[BaseModel]:
        """Return the concrete Pydantic output model for this step class."""

        return _resolve_models(cls)[1]

    @abstractmethod
    def run(self, inputs: TInput, context: ExecutionContext) -> TOutput:
        """Execute the step's typed business logic.

        Args:
            inputs: Already-constructed, typed input parameters.
            context: Transport-independent execution context.

        Returns:
            The step's typed output.
        """

    def execute(self, inputs: TInput, context: ExecutionContext) -> TOutput:
        """Execute a typed step through the stable subclass hook.

        Args:
            inputs: Already-constructed, typed input parameters.
            context: Transport-independent execution context.

        Returns:
            The step's typed output.
        """

        return self.run(inputs, context)


class ActionStep[TInput: BaseModel, TOutput: BaseModel](BaseNode[TInput, TOutput]):
    """Executable base for Action steps (domain and external integrations).

    Actions are workflow step types that integrate with external systems or
    perform domain operations. They remain directly callable from Python and do
    not require a container invocation.
    """


class TaskStep[TInput: BaseModel, TOutput: BaseModel](BaseNode[TInput, TOutput]):
    """Executable base for Task steps (atomic compute operations).

    Tasks perform self-contained computation, scripting, or data
    transformation. A missing Task source-manifest schema does not prevent
    direct SDK execution with a valid runtime descriptor.
    """


class WorkflowStep[TInput: BaseModel, TOutput: BaseModel](BaseNode[TInput, TOutput]):
    """Executable base for Workflow steps (control-flow logic).

    Workflow steps implement control flow or composition. They share the same
    typed lifecycle as the other taxonomy classes and remain callable without
    transport types.
    """


class TriggerStep[TInput: BaseModel, TOutput: BaseModel](BaseNode[TInput, TOutput]):
    """Executable base for Trigger steps (event entry points).

    Triggers start workflows in response to events. Direct SDK execution
    exercises the typed implementation hook; platform event delivery and
    workflow instantiation are outside this class.
    """
