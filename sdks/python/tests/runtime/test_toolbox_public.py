"""Public Toolbox behavior in execution contexts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pydantic import BaseModel
from syntara_plugin.runtime import (
    ContainerInvocation,
    ContainerToolbox,
    ControlStep,
    ExecutionContext,
)
from syntara_plugin.runtime.adapter import _execution_context
from syntara_plugin.runtime.credential_callback import CallbackCredentialProvider
from syntara_plugin.runtime.toolbox import _toolbox_active, _toolbox_state
from syntara_plugin.testing import build_execution_context, invoke_step, invoke_step_in_container


class ToolboxInput(BaseModel):
    pass


class ToolboxOutput(BaseModel):
    present: bool
    empty: bool


class ToolboxProbe(ControlStep[ToolboxInput, ToolboxOutput]):
    def run(self, inputs: ToolboxInput, context: ExecutionContext) -> ToolboxOutput:
        return ToolboxOutput(
            present=isinstance(context.toolbox, ContainerToolbox),
            empty=callable(context.toolbox.log),
        )


def test_context_exposes_a_stable_read_only_toolbox() -> None:
    context = ExecutionContext("example")
    toolbox = context.toolbox
    assert isinstance(toolbox, ContainerToolbox)
    assert context.toolbox is toolbox
    assert context._with_credentials(()).toolbox is toolbox
    assert ExecutionContext("another").toolbox is not toolbox
    assert [name for name in dir(toolbox) if not name.startswith("_")] == ["log"]
    assert not hasattr(toolbox, "resolve_credential_ref")
    assert not hasattr(toolbox, "request_credential")
    assert not hasattr(toolbox, "_transport")
    assert not hasattr(toolbox, "_bind")
    with pytest.raises(FrozenInstanceError):
        setattr(context, "toolbox", ContainerToolbox())  # noqa: B010
    with pytest.raises(AttributeError):
        setattr(toolbox, "execute", lambda: None)  # noqa: B010


@pytest.mark.parametrize("container", [False, True])
def test_toolbox_is_available_on_both_execution_paths(container: bool) -> None:
    context = build_execution_context(invocation_id="toolbox-test")
    invoke = invoke_step_in_container if container else invoke_step
    outcome = invoke(ToolboxProbe(), {}, context=context)
    assert outcome.ok
    assert outcome.output == {"present": True, "empty": True}


class RetainingProbe(ControlStep[ToolboxInput, ToolboxOutput]):
    def __init__(self, fail: bool = False) -> None:
        self.retained: ContainerToolbox | None = None
        self.fail = fail

    def run(self, inputs: ToolboxInput, context: ExecutionContext) -> ToolboxOutput:
        self.retained = context.toolbox
        assert _toolbox_active(self.retained)
        if self.fail:
            raise RuntimeError("step failed")
        return ToolboxOutput(present=True, empty=True)


@pytest.mark.parametrize("container", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_toolbox_expires_after_step_returns_or_fails(container: bool, fail: bool) -> None:
    probe = RetainingProbe(fail)
    invoke = invoke_step_in_container if container else invoke_step
    context = build_execution_context(invocation_id="lifecycle-test")
    outcome = invoke(probe, {}, context=context)
    assert outcome.ok is not fail
    assert probe.retained is not None
    assert probe.retained is not context.toolbox
    assert _toolbox_active(context.toolbox)
    assert _toolbox_state(probe.retained).closed.is_set()
    assert not _toolbox_active(probe.retained)


@pytest.mark.parametrize("container", [False, True])
def test_reused_context_receives_a_fresh_toolbox_for_each_invocation(container: bool) -> None:
    context = build_execution_context(invocation_id="reused-toolbox")
    invoke = invoke_step_in_container if container else invoke_step
    first = RetainingProbe()
    second = RetainingProbe()

    assert invoke(first, {}, context=context).ok
    assert invoke(second, {}, context=context).ok

    assert first.retained is not None
    assert second.retained is not None
    assert first.retained is not second.retained
    assert first.retained is not context.toolbox
    assert second.retained is not context.toolbox
    assert not _toolbox_active(first.retained)
    assert not _toolbox_active(second.retained)
    assert _toolbox_active(context.toolbox)


def test_toolbox_observes_cooperative_cancellation() -> None:
    context = ExecutionContext("cancelled")
    assert _toolbox_active(context.toolbox)
    context.cancellation.set()
    assert not _toolbox_active(context.toolbox)


def test_container_context_shares_only_its_own_invocation_toolbox() -> None:
    from threading import Event

    providers = [CallbackCredentialProvider(str(i), lambda request: None, Event(), 5) for i in range(2)]
    contexts = [
        _execution_context(
            ContainerInvocation(
                invocation_id=str(i), workflow_run_id="workflow", step_revision_id="step",
                inputs={}, timeout_seconds=5, credential_provider=provider,
            )
        )
        for i, provider in enumerate(providers)
    ]
    assert contexts[0].toolbox is providers[0]._public_toolbox
    assert contexts[1].toolbox is providers[1]._public_toolbox
    assert contexts[0].toolbox is not contexts[1].toolbox
    providers[0].close()
    assert not _toolbox_active(contexts[0].toolbox)
    assert _toolbox_active(contexts[1].toolbox)
    providers[1].close()
