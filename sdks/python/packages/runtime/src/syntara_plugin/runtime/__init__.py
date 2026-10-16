"""Runtime package boundary for Syntara plugins.

Direct, transport-independent execution (typed steps, bindings, the output
envelope) imports without pulling in gRPC or the generated protobuf. The
container transport symbols, which depend on those libraries, are exported
lazily through :pep:`562` module ``__getattr__`` and resolve only on first use.
"""

import importlib
from typing import TYPE_CHECKING, Any

from syntara_plugin.contracts import ContractBundle

from .abi import (
    CONTAINER_ABI,
    PROVIDER_ABI,
    ConformanceHarness,
    ConformanceResult,
    FailureCode,
    RuntimeBinding,
    RuntimeOwnership,
    validate_provider,
    validate_runtime,
)
from .action_validation import ActionInputValidationError
from .bindings import (
    InstalledActionManifest,
    StepBinding,
    StepBindingError,
    StepBindings,
    StepDescriptor,
    StepTaxonomy,
)
from .context import ExecutionContext
from .contract_validation import (
    DriftCode,
    DriftIssue,
    DriftReport,
    DriftSeverity,
    EntryPointResolver,
    SchemaDriftError,
    check_plugin_bindings,
    check_step_contract,
    check_target_binding,
    compare_model_to_schema,
    validate_plugin_bindings,
)
from .execution import StepExecutionError, StepExecutor
from .http import (
    HttpOperationError,
    HttpOperationMapper,
    HttpRequest,
    HttpRunnerContext,
    HttpRunnerContextError,
    build_http_request,
    load_http_runner_context,
    map_http_response,
)
from .outcome import ErrorMessage, StandardOutputWrapper
from .step import ActionStep, BaseStep, TaskStep, TriggerStep, WorkflowStep
from .validation import StepInputValidationError

if TYPE_CHECKING:
    from .adapter import (
        ResultObjectStore,
        container_action_for_step,
        container_step_for_binding,
        container_step_for_bindings,
        step_registry_from_bindings,
    )
    from .container import (
        ANY_STEP_REVISION,
        ContainerClient,
        ContainerInvocation,
        ContainerInvocationError,
        ContainerProgress,
        ContainerRemoteError,
        ContainerResult,
        ContainerRuntimeService,
        ContainerStepHandler,
        StepRegistry,
        add_container_runtime_service,
    )

# Transport exports whose defining module imports gRPC/protobuf. They are loaded
# on first attribute access so importing this package stays transport-free.
_LAZY_EXPORTS = {
    "ANY_STEP_REVISION": ".container",
    "ContainerClient": ".container",
    "ContainerInvocation": ".container",
    "ContainerInvocationError": ".container",
    "ContainerProgress": ".container",
    "ContainerRemoteError": ".container",
    "ContainerResult": ".container",
    "ContainerRuntimeService": ".container",
    "ContainerStepHandler": ".container",
    "StepRegistry": ".container",
    "add_container_runtime_service": ".container",
    "ResultObjectStore": ".adapter",
    "container_action_for_step": ".adapter",
    "container_step_for_binding": ".adapter",
    "container_step_for_bindings": ".adapter",
    "step_registry_from_bindings": ".adapter",
}


def __getattr__(name: str) -> Any:
    """Resolve transport exports lazily (:pep:`562`)."""

    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        message = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(message)
    module = importlib.import_module(module_name, __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "ANY_STEP_REVISION",
    "ActionInputValidationError",
    "ActionStep",
    "BaseStep",
    "CONTAINER_ABI",
    "ConformanceHarness",
    "ConformanceResult",
    "ContainerClient",
    "ContainerInvocation",
    "ContainerInvocationError",
    "ContainerProgress",
    "ContainerRemoteError",
    "ContainerResult",
    "ContainerRuntimeService",
    "ContainerStepHandler",
    "ContractBundle",
    "DriftCode",
    "DriftIssue",
    "DriftReport",
    "DriftSeverity",
    "EntryPointResolver",
    "ErrorMessage",
    "ExecutionContext",
    "FailureCode",
    "HttpOperationError",
    "HttpOperationMapper",
    "HttpRequest",
    "HttpRunnerContext",
    "HttpRunnerContextError",
    "InstalledActionManifest",
    "PROVIDER_ABI",
    "ResultObjectStore",
    "RuntimeBinding",
    "RuntimeOwnership",
    "SchemaDriftError",
    "StandardOutputWrapper",
    "StepBinding",
    "StepBindingError",
    "StepBindings",
    "StepDescriptor",
    "StepExecutionError",
    "StepExecutor",
    "StepInputValidationError",
    "StepRegistry",
    "StepTaxonomy",
    "TaskStep",
    "TriggerStep",
    "WorkflowStep",
    "add_container_runtime_service",
    "build_http_request",
    "check_plugin_bindings",
    "check_step_contract",
    "check_target_binding",
    "compare_model_to_schema",
    "container_action_for_step",
    "container_step_for_binding",
    "container_step_for_bindings",
    "load_http_runner_context",
    "map_http_response",
    "step_registry_from_bindings",
    "validate_plugin_bindings",
    "validate_provider",
    "validate_runtime",
]
