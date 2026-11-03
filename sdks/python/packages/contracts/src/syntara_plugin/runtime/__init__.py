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
from .bindings import (
    InstalledStepManifest,
    StepBinding,
    StepBindingError,
    StepBindings,
    StepCategory,
    StepDescriptor,
    StepKind,
    to_json_document,
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
from .execution import StepExecutionError, StepExecutor, StepFailure, invoke
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
from .manifest_validation import ManifestInputValidationError
from .outcome import (
    ErrorCategory,
    ErrorMessage,
    ErrorResult,
    StandardOutputWrapper,
    error_result_base_schema,
    error_result_schema,
)
from .plugin_bindings import load_plugin_bindings
from .step import ActionStep, BaseStep, ControlStep, TriggerStep
from .validation import StepInputValidationError

if TYPE_CHECKING:
    from .adapter import (
        ResultObjectStore,
        container_step_for_binding,
        container_step_for_bindings,
        container_step_for_installed_manifest,
        step_registry_from_bindings,
    )
    from .container import (
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
        standard_output_from_container_result,
    )

# Transport exports whose defining module imports gRPC/protobuf. They are loaded
# on first attribute access so importing this package stays transport-free.
_CONTAINER_MODULE = ".container"
_ADAPTER_MODULE = ".adapter"

_LAZY_EXPORTS = {
    "ContainerClient": _CONTAINER_MODULE,
    "ContainerInvocation": _CONTAINER_MODULE,
    "ContainerInvocationError": _CONTAINER_MODULE,
    "ContainerProgress": _CONTAINER_MODULE,
    "ContainerRemoteError": _CONTAINER_MODULE,
    "ContainerResult": _CONTAINER_MODULE,
    "ContainerRuntimeService": _CONTAINER_MODULE,
    "ContainerStepHandler": _CONTAINER_MODULE,
    "StepRegistry": _CONTAINER_MODULE,
    "add_container_runtime_service": _CONTAINER_MODULE,
    "standard_output_from_container_result": _CONTAINER_MODULE,
    "ResultObjectStore": _ADAPTER_MODULE,
    "container_step_for_installed_manifest": _ADAPTER_MODULE,
    "container_step_for_binding": _ADAPTER_MODULE,
    "container_step_for_bindings": _ADAPTER_MODULE,
    "step_registry_from_bindings": _ADAPTER_MODULE,
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
    "ManifestInputValidationError",
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
    "ErrorCategory",
    "EntryPointResolver",
    "ErrorMessage",
    "ErrorResult",
    "error_result_base_schema",
    "error_result_schema",
    "ExecutionContext",
    "FailureCode",
    "HttpOperationError",
    "HttpOperationMapper",
    "HttpRequest",
    "HttpRunnerContext",
    "HttpRunnerContextError",
    "InstalledStepManifest",
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
    "StepFailure",
    "StepInputValidationError",
    "StepRegistry",
    "StepCategory",
    "StepKind",
    "to_json_document",
    "TriggerStep",
    "ControlStep",
    "add_container_runtime_service",
    "build_http_request",
    "check_plugin_bindings",
    "check_step_contract",
    "check_target_binding",
    "compare_model_to_schema",
    "container_step_for_installed_manifest",
    "container_step_for_binding",
    "container_step_for_bindings",
    "load_http_runner_context",
    "load_plugin_bindings",
    "map_http_response",
    "invoke",
    "step_registry_from_bindings",
    "standard_output_from_container_result",
    "validate_plugin_bindings",
    "validate_provider",
    "validate_runtime",
]
