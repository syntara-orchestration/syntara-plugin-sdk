"""Pytest plugin distributing the Syntara step-testing fixture suite.

The fixtures are thin wrappers over the importable function core in this
package, so the same behavior is available with or without pytest. Fixture
names are public API with a deprecation policy; there are deliberately no
autouse fixtures.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from syntara_plugin.runtime import ExecutionContext
from syntara_plugin.sdk import CompilationResult

from . import manifests
from .bindings_factory import StepBindingsFactory
from .context_builder import build_execution_context
from .manifests import ManifestValidator


@pytest.fixture
def manifest_validator() -> ManifestValidator:
    """Factory fixture validating manifests against the canonical schema bundle."""

    return manifests.ManifestValidator()


@pytest.fixture
def compile_manifest() -> Callable[..., CompilationResult]:
    """Factory fixture compiling a plugin workspace or in-memory plugin model."""

    return manifests.compile_manifest


@pytest.fixture
def mock_execution_context() -> Callable[..., ExecutionContext]:
    """Factory fixture building locally approved ``ExecutionContext`` objects."""

    return build_execution_context


@pytest.fixture
def step_bindings() -> StepBindingsFactory:
    """Factory fixture building drift-verified step bindings.

    Use ``step_bindings.bind(step)`` for any step (a locally installed
    manifest) and ``step_bindings.bind_step(entry_point, workspace=...)``
    for custom-runner Step types compiled from a real workspace.
    """

    return StepBindingsFactory()
