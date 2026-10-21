"""pytester-based end-to-end tests for the pytest11 entry-point plugin.

These tests simulate a fresh author project: the fixtures must be discovered
by name in an isolated pytest run with no conftest wiring, and must not change
behavior for tests that never request them.
"""

from __future__ import annotations

import pytest


def test_fixtures_are_auto_discovered_in_a_fresh_project(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        from syntara_plugin.testing import ReferenceControlStep, invoke_step

        def test_reference_step(mock_execution_context):
            outcome = invoke_step(
                ReferenceControlStep(), {"title": "hi"}, context=mock_execution_context()
            )
            assert outcome.ok
            assert outcome.run_calls == 1

        def test_bindings_factory(step_bindings):
            binding = step_bindings.bind(ReferenceControlStep)
            assert step_bindings.resolve(
                "syntara_plugin.testing.payloads.ReferenceControlStep"
            ).descriptor.step_identity == binding.descriptor.step_identity
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=2)


def test_fixtures_are_opt_in_with_no_side_effects(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        def test_unrelated():
            assert 1 + 1 == 2
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)


def test_container_helper_works_in_a_fresh_project(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        from syntara_plugin.testing import (
            ReferenceControlStep,
            assert_outcomes_equivalent,
            invoke_step,
            invoke_step_in_container,
        )

        def test_equivalence(mock_execution_context):
            context = mock_execution_context(invocation_id="pytester-equivalence")
            direct = invoke_step(ReferenceControlStep(), {"title": "hi"}, context=context)
            container = invoke_step_in_container(
                ReferenceControlStep(), {"title": "hi"}, context=context
            )
            assert_outcomes_equivalent(direct, container)
        """
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)


def test_fixture_vocabulary_is_stable() -> None:
    import inspect

    from syntara_plugin.testing import plugin

    source = inspect.getsource(plugin)
    for name in (
        "manifest_validator",
        "compile_manifest",
        "mock_execution_context",
        "step_bindings",
    ):
        assert f"def {name}" in source, name
