"""Local pytest harness and fixtures for Syntara plugin step authors.

The function core is importable without pytest; the ``pytest11`` entry point
additionally distributes four fixtures (``manifest_validator``,
``compile_manifest``, ``mock_execution_context``, ``step_bindings``) that wrap
that core. Every helper routes through the real SDK executor, validation, and
result wrapping; nothing reimplements platform contracts.
"""

from __future__ import annotations

from .bindings_factory import StepBindingsFactory
from .chaining import ChainOutcome, chain_steps, check_seam_compatible, identity_seam
from .conformance import (
    ConformanceFailure,
    ConformanceReport,
    check_invalid_blocks_hook,
    check_paths_equivalent,
    check_step_conformance,
    check_valid_reaches_hook_once,
)
from .context_builder import build_execution_context
from .equivalence import assert_outcomes_equivalent
from .errors import HarnessUsageError
from .invocation import (
    BoundedStepHandler,
    StepOutcome,
    invoke_step,
    invoke_step_in_container,
)
from .loader import (
    entry_point_of,
    load_step_class,
    parse_entry_point,
    resolve_step,
)
from .manifests import (
    ManifestValidationError,
    ManifestValidator,
    compile_manifest,
)
from .payloads import (
    DELIBERATE_FAILURE_CASES,
    FIXED_SECRET_SENTINEL,
    INVALID_INPUT_CASES,
    INVALID_OUTPUT_CASES,
    PAYLOAD_CASES,
    VALID_INPUT_CASES,
    Expectation,
    FailingReferenceStep,
    PayloadCase,
    ReferenceControlStep,
    ReferenceInput,
    ReferenceOutput,
    schema_violating_binding,
)
from .redaction import SecretLeakError, assert_no_secrets, secret_sentinel

__all__ = [
    "BoundedStepHandler",
    "ConformanceFailure",
    "ConformanceReport",
    "ChainOutcome",
    "DELIBERATE_FAILURE_CASES",
    "Expectation",
    "FIXED_SECRET_SENTINEL",
    "FailingReferenceStep",
    "HarnessUsageError",
    "INVALID_INPUT_CASES",
    "INVALID_OUTPUT_CASES",
    "ManifestValidationError",
    "ManifestValidator",
    "PAYLOAD_CASES",
    "PayloadCase",
    "ReferenceInput",
    "ReferenceOutput",
    "ReferenceControlStep",
    "StepBindingsFactory",
    "StepOutcome",
    "VALID_INPUT_CASES",
    "assert_no_secrets",
    "assert_outcomes_equivalent",
    "build_execution_context",
    "chain_steps",
    "check_invalid_blocks_hook",
    "check_paths_equivalent",
    "check_seam_compatible",
    "check_step_conformance",
    "check_valid_reaches_hook_once",
    "compile_manifest",
    "entry_point_of",
    "identity_seam",
    "invoke_step",
    "invoke_step_in_container",
    "load_step_class",
    "parse_entry_point",
    "resolve_step",
    "schema_violating_binding",
    "secret_sentinel",
    "SecretLeakError",
]
