"""Credential metadata and invocation-scoped capability contracts."""

from __future__ import annotations

import json
import logging
import pickle
import traceback
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from queue import Queue
from typing import Any, ClassVar

import grpc
import pytest
import syntara_plugin.runtime.credentials as credentials_module
from pydantic import BaseModel, ValidationError
from syntara_plugin.runtime import (
    ActionStep,
    ContainerInvocation,
    ContainerInvocationError,
    ContainerRemoteError,
    ControlStep,
    CredentialAccessError,
    CredentialBinding,
    CredentialRecipe,
    ExecutionContext,
    FailureCode,
    FileCredentialProvider,
    InstalledStepManifest,
    IntegrationType,
    StepBinding,
    StepBindingError,
    StepDescriptor,
    StepExecutionError,
    StepExecutor,
    StepFailure,
    StepRegistry,
    TriggerStep,
    add_container_runtime_service,
    container_step_for_binding,
    invoke,
)
from syntara_plugin.runtime.container import _request_from_invocation
from syntara_plugin.runtime.protocol import container_pb2, container_pb2_grpc
from syntara_plugin.testing import build_execution_context, invoke_step, invoke_step_in_container

_SECRET = "sample-credential-value-keep-private"
_NAME = "acme.demo.token"
_OTHER = "acme.demo.other"
_KEY_SECRET = "private-mapping-key-sentinel"


class CredentialInput(BaseModel):
    mode: str


class CredentialOutput(BaseModel):
    result: str | int


class CredentialStep(ActionStep[CredentialInput, CredentialOutput]):
    saved: ClassVar[CredentialBinding | None] = None
    saved_provider: ClassVar[Any] = None

    def run(self, inputs: CredentialInput, context: ExecutionContext) -> CredentialOutput:
        try:
            if inputs.mode == "system_exit_before":
                raise SystemExit("pre-read")
            if inputs.mode == "keyboard_interrupt_before":
                raise KeyboardInterrupt("pre-read")
            scope = context._credential_scope
            assert scope is not None
            if inputs.mode == "scope_read":
                scope.read(_OTHER)
            elif inputs.mode == "provider_read":
                assert scope._provider is not None
                scope._provider.read(_OTHER)
            elif inputs.mode == "inner_provider_read":
                assert scope._provider is not None
                inner = getattr(scope._provider, "_ScopedCredentialProvider__provider")  # noqa: B009
                inner.read(_OTHER)
            elif inputs.mode == "rescope":
                context._with_credentials((_OTHER,)).credential(_OTHER).read()
            if inputs.mode in {"provider_allowed_leak", "inner_allowed_leak"}:
                assert scope._provider is not None
                provider = (
                    getattr(scope._provider, "_ScopedCredentialProvider__provider")  # noqa: B009
                    if inputs.mode == "inner_allowed_leak" else scope._provider
                )
                type(self).saved_provider = provider
                credential = provider.read(_NAME)
            else:
                handle = context.credential(_OTHER if inputs.mode == "undeclared" else _NAME)
                type(self).saved = handle
                credential = handle.read()
            if inputs.mode == "exception":
                raise ValueError(str(credential["token"]))
            if inputs.mode == "terminal_failure":
                raise ContainerInvocationError(
                    FailureCode.REMOTE_REJECTED,
                    "The tracker rejected the request.",
                    retryable=True,
                    outcome="not_completed",
                    provider_code="tracker",
                )
            if inputs.mode == "terminal_failure_leak":
                raise ContainerInvocationError(
                    FailureCode.REMOTE_REJECTED,
                    f"token={credential['token']}",
                    retryable=True,
                    outcome="not_completed",
                )
            if inputs.mode == "terminal_failure_provider_leak":
                raise ContainerInvocationError(
                    FailureCode.REMOTE_REJECTED,
                    "The tracker rejected the request.",
                    retryable=True,
                    outcome="not_completed",
                    provider_code=str(credential["token"]),
                ) from ValueError(str(credential["token"]))
            if inputs.mode == "terminal_failure_detail_leak":
                raise ContainerInvocationError(
                    FailureCode.REMOTE_REJECTED,
                    "The tracker rejected the request.",
                    retryable=True,
                    outcome="not_completed",
                    detail=f"provider said: {credential['token']}",
                ) from ValueError(f"upstream failed: {credential['token']}")
            if inputs.mode == "terminal_failure_mutated":
                failure = ContainerInvocationError(
                    FailureCode.REMOTE_REJECTED,
                    "The tracker rejected the request.",
                    retryable=True,
                    outcome="not_completed",
                )
                failure.provider_code = "Not A Code!"
                raise failure from ValueError(f"upstream failed: {credential['token']}")
            if inputs.mode == "system_exit":
                raise SystemExit(credential["token"])
            if inputs.mode == "keyboard_interrupt":
                raise KeyboardInterrupt(credential["token"])
            if inputs.mode in {"leak", "provider_allowed_leak", "inner_allowed_leak"}:
                return CredentialOutput(result=f"token={credential['token']}")
            if inputs.mode == "number_raw_leak":
                return CredentialOutput(result=credential["pin"])
            if inputs.mode == "numeric_token_leak":
                return CredentialOutput(result=int(credential["numeric_token"]))
            if inputs.mode == "large_output":
                return CredentialOutput(result="x" * 1_000)
            if inputs.mode in {"number_leak", "boolean_leak", "key_leak"}:
                value = {
                    "number_leak": lambda: str(credential["pin"]),
                    "boolean_leak": lambda: str(credential["flag"]),
                    "key_leak": lambda: next(iter(credential["nested"])),
                }[inputs.mode]()
                return CredentialOutput(result=value)
            if inputs.mode == "status_code_leak":
                raise StepFailure(credential["pin"], "Failed", "Safe failure")
            if inputs.mode == "numeric_token_status_code_leak":
                raise StepFailure(int(credential["numeric_token"]), "Failed", "Safe failure")
            if inputs.mode in {"number_failure", "boolean_failure"}:
                value = credential["pin"] if inputs.mode == "number_failure" else credential["flag"]
                raise StepFailure(1, "Failed", f"credential: {value}")
            return CredentialOutput(result="accepted")
        except CredentialAccessError as error:
            return CredentialOutput(result=error.reason)


class ControlCredentialStep(ControlStep[CredentialInput, CredentialOutput]):
    def run(self, inputs: CredentialInput, context: ExecutionContext) -> CredentialOutput:
        token = context.credential(_NAME).read()["token"]
        return CredentialOutput(result="accepted" if token == _SECRET else "other")


class TriggerCredentialStep(TriggerStep[CredentialInput, CredentialOutput]):
    def run(self, inputs: CredentialInput, context: ExecutionContext) -> CredentialOutput:
        token = context.credential(_NAME).read()["token"]
        return CredentialOutput(result="accepted" if token == _SECRET else "other")


def _connection_targets() -> list[dict[str, Any]]:
    metadata = {"name": "tracker", "displayName": "Tracker", "description": "Tracker."}
    return [
        {"apiVersion": "syntara.io/v1alpha1", "kind": "IntegrationType", "metadata": metadata,
         "spec": {"id": "acme.demo.integration", "endpointKinds": ["https"],
                  "credentialTypes": [_NAME], "configuration": {"kind": "inline", "value": {}}}},
        {"apiVersion": "syntara.io/v1alpha1", "kind": "CredentialRecipe", "metadata": metadata,
         "spec": {"id": _NAME, "integrationType": "acme.demo.integration",
                  "credentialSchema": {"kind": "inline", "value": {"type": "object"}},
                  "secretFields": ["token"], "issuance": {"kind": "static-bearer.v1"}}},
    ]


def _binding(targets: list[dict[str, Any]] | None = None) -> StepBinding:
    step = CredentialStep()
    manifest = {
        "apiVersion": "syntara.io/v1alpha1", "kind": "Action",
        "metadata": {"name": "use", "displayName": "Use", "description": "Use a credential."},
        "spec": {
            "runtime": {"kind": "custom-runner", "entryPoint": f"{__name__}:CredentialStep"},
            "integrationType": "acme.demo.integration",
            "input": {"kind": "inline", "value": CredentialInput.model_json_schema()},
            "output": {"kind": "inline", "value": CredentialOutput.model_json_schema()},
        },
    }
    installed = InstalledStepManifest.from_manifest(
        "acme.demo.use", manifest, {}, credential_targets=targets if targets is not None else _connection_targets()
    )
    return StepBinding(StepDescriptor.for_installed_step(step, installed), step)


@pytest.mark.parametrize(
    "kind,step_class,kind_spec",
    [
        ("Control", ControlCredentialStep, {"invocation": {"kind": "orchestration"}}),
        ("Trigger", TriggerCredentialStep, {
            "driver": "webhook.v1", "configuration": {"kind": "inline", "value": {"type": "object"}}
        }),
    ],
)
def test_control_and_trigger_read_declared_credentials(
    tmp_path: Path, kind: str, step_class: type[ControlCredentialStep] | type[TriggerCredentialStep],
    kind_spec: dict[str, Any],
) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": _SECRET}))
    spec = {
        "runtime": {"kind": "custom-runner", "entryPoint": f"{__name__}:{step_class.__name__}"},
        "integrationType": "acme.demo.integration",
        "input": {"kind": "inline", "value": CredentialInput.model_json_schema()},
        "output": {"kind": "inline", "value": CredentialOutput.model_json_schema()},
        **kind_spec,
    }
    manifest = {
        "apiVersion": "syntara.io/v1alpha1", "kind": kind,
        "metadata": {"name": "use", "displayName": "Use", "description": "Use a credential."},
        "spec": spec,
    }
    step = step_class()
    installed = InstalledStepManifest.from_manifest(
        "acme.demo.use", manifest, {}, credential_targets=_connection_targets()
    )
    binding = StepBinding(StepDescriptor.for_installed_step(step, installed), step)
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    for call in (invoke_step, invoke_step_in_container):
        outcome = call(binding, {"mode": "use"}, context=context)
        assert outcome.ok
        assert outcome.output == {"result": "accepted"}


def test_canonical_models_round_trip_and_reject_unversioned_extensions() -> None:
    targets = _connection_targets()
    assert IntegrationType.model_validate(targets[0]).model_dump(mode="json") == targets[0]
    for issuance in (
        {"kind": "static-bearer.v1"},
        {"kind": "oauth2-authorization-code.v1", "scopes": ["read"], "pkce": True},
        {"kind": "jwt-token-exchange.v1", "audience": "service"},
    ):
        targets[1]["spec"]["issuance"] = issuance
        assert CredentialRecipe.model_validate(targets[1]).model_dump(mode="json") == targets[1]
    targets[1]["spec"]["authenticate"] = {"kind": "custom"}
    with pytest.raises(ValidationError):
        CredentialRecipe.model_validate(targets[1])


def test_trusted_graph_rejects_missing_and_mismatched_credentials() -> None:
    targets = _connection_targets()
    with pytest.raises(StepBindingError):
        _binding(targets[:1])
    targets[1]["spec"]["integrationType"] = "acme.other.integration"
    with pytest.raises(StepBindingError):
        _binding(targets)
    binding = _binding()
    object.__setattr__(binding.descriptor, "credential_names", ("acme.demo.other",))
    with pytest.raises(StepBindingError):
        binding.descriptor.require_executable_contract(binding.step)


def test_approved_file_capability_has_direct_and_container_parity(tmp_path: Path, caplog: Any) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": _SECRET}))
    context = build_execution_context(
        invocation_id="credential-test", credential_file_paths={_NAME: str(path)},
        credential_directory=tmp_path,
    )
    binding = _binding()
    for call in (invoke_step, invoke_step_in_container):
        outcome = call(binding, {"mode": "use"}, context=context)
        assert outcome.ok
        assert outcome.output == {"result": "accepted"}
        assert _SECRET not in repr(outcome)
        assert _SECRET not in repr(context)
        with pytest.raises(TypeError) as serialization_error:
            asdict(context)
        assert _SECRET not in str(serialization_error.value)
        handle = CredentialStep.saved
        assert handle is not None
        assert _SECRET not in repr(handle)
        with pytest.raises(TypeError):
            pickle.dumps(handle)
        with pytest.raises(CredentialAccessError, match="expired"):
            handle.read()
    assert _SECRET not in caplog.text


@pytest.mark.parametrize("mode,expected", [("undeclared", "undeclared"), ("use", "missing")])
def test_undeclared_and_missing_fail_safely(mode: str, expected: str) -> None:
    outcome = invoke_step(_binding(), {"mode": mode})
    assert outcome.output == {"result": expected}


def test_denied_missing_file_and_secret_leaks_are_safe(tmp_path: Path) -> None:
    class DeniedView:
        def read(self, name: str) -> dict[str, Any]:
            raise CredentialAccessError("unauthorized")

        def close(self) -> None:
            pass

    class DeniedProvider:
        def restrict(
            self, names: frozenset[str], on_read: Callable[[Mapping[str, Any]], None]
        ) -> DeniedView:
            return DeniedView()

    denied = ExecutionContext(invocation_id="denied", credential_provider=DeniedProvider())
    assert invoke_step(_binding(), {"mode": "use"}, context=denied).output == {
        "result": "unauthorized"
    }
    path = tmp_path / "credential"
    provider = FileCredentialProvider({_NAME: str(path)}, local_directory=tmp_path)
    context = ExecutionContext(invocation_id="use", credential_provider=provider)
    assert invoke_step(_binding(), {"mode": "use"}, context=context).output == {
        "result": "unavailable"
    }
    path.write_text(json.dumps({"token": _SECRET}))
    for mode in ("exception", "leak"):
        outcome = invoke_step(_binding(), {"mode": mode}, context=context)
        assert not outcome.ok
        assert outcome.raw is None
        assert _SECRET not in repr(outcome.raised)
        assert _SECRET not in str(getattr(outcome.raised, "detail", ""))
        binding = _binding()
        with pytest.raises(StepExecutionError) as error:
            invoke(binding, {"mode": mode}, context)
        assert _SECRET not in str(error.value)


def _terminal_failure_event(
    binding: StepBinding, inputs: Mapping[str, Any], credential_path: Path, root: Path
) -> Any:
    """Exercise sensitive responses separately from terminal business events."""
    del root
    invocation = ContainerInvocation(
        invocation_id="terminal-leak-probe", workflow_run_id="terminal-leak-probe-run",
        step_revision_id=binding.descriptor.step_identity, inputs=dict(inputs), timeout_seconds=30,
    )
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    add_container_runtime_service(server, StepRegistry({
        binding.descriptor.step_identity: container_step_for_binding(binding)
    }))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    queue: Queue[Any] = Queue()
    queue.put(container_pb2.ExecuteClientMessage(start=_request_from_invocation(invocation)))

    def messages():
        while (message := queue.get()) is not None:
            yield message

    try:
        call = container_pb2_grpc.ContainerServiceStub(channel).Execute(messages())
        for message in call:
            if message.WhichOneof("message") == "credential_request":
                request = message.credential_request
                assert request.name == _NAME
                queue.put(container_pb2.ExecuteClientMessage(credential_response=container_pb2.CredentialResponse(
                    invocation_id=request.invocation_id, request_id=request.request_id,
                    document_json=credential_path.read_bytes(),
                )))
            else:
                return message.event
        raise AssertionError("missing terminal event")
    finally:
        queue.put(None)
        channel.close()
        server.stop(0).wait()


def test_a_terminal_failure_after_a_read_keeps_its_fields(tmp_path: Path) -> None:
    """The credential scan must not degrade a clean deliberate terminal failure."""

    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": _SECRET}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    direct = invoke_step(_binding(), {"mode": "terminal_failure"}, context=context)
    assert isinstance(direct.raised, ContainerInvocationError)
    assert direct.raised.code is FailureCode.REMOTE_REJECTED
    assert direct.raised.safe_message == "The tracker rejected the request."
    assert direct.raised.retryable is True
    assert direct.raised.outcome == "not_completed"
    assert direct.raised.provider_code == "tracker"
    container = invoke_step_in_container(
        _binding(), {"mode": "terminal_failure"}, context=context
    )
    assert isinstance(container.raised, ContainerRemoteError)
    assert container.raised.source == "workload"
    assert container.raised.code is FailureCode.REMOTE_REJECTED
    assert container.raised.message == "The tracker rejected the request."
    assert container.raised.retryable is True
    assert container.raised.outcome == "not_completed"
    assert container.raised.provider_code == "tracker"
    event = _terminal_failure_event(_binding(), {"mode": "terminal_failure"}, path, tmp_path)
    assert event.WhichOneof("event") == "failure"
    assert event.failure.code == "REMOTE_REJECTED"
    assert event.failure.provider_code == "tracker"
    assert _SECRET not in str(event)


def test_a_terminal_failure_provider_code_cannot_leak_a_read_credential(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": _SECRET}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    outcomes = (
        invoke_step(_binding(), {"mode": "terminal_failure_provider_leak"}, context=context),
        invoke_step_in_container(
            _binding(), {"mode": "terminal_failure_provider_leak"}, context=context
        ),
    )
    for outcome in outcomes:
        assert not outcome.ok
        assert outcome.raw is None
        code = (
            outcome.raised.failure_code
            if isinstance(outcome.raised, StepExecutionError)
            else outcome.raised.code
        )
        assert code is FailureCode.ABI_VIOLATION
        assert _SECRET not in repr(outcome)
        assert _SECRET not in str(outcome.raised)
        assert _SECRET not in "".join(traceback.format_exception(outcome.raised))
        if isinstance(outcome.raised, StepExecutionError):
            # The degraded failure must not retain the original, whose
            # provider code and cause carry the credential, as its context.
            assert outcome.raised.__context__ is None
    event = _terminal_failure_event(
        _binding(), {"mode": "terminal_failure_provider_leak"}, path, tmp_path
    )
    assert event.WhichOneof("event") == "failure"
    assert event.failure.code == "ABI_VIOLATION"
    assert _SECRET not in str(event)


def test_a_terminal_failure_detail_and_chain_cannot_leak_a_read_credential(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": _SECRET}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    direct = invoke_step(_binding(), {"mode": "terminal_failure_detail_leak"}, context=context)
    assert isinstance(direct.raised, ContainerInvocationError)
    assert direct.raised.code is FailureCode.REMOTE_REJECTED
    assert direct.raised.safe_message == "The tracker rejected the request."
    assert direct.raised.retryable is True
    assert direct.raised.outcome == "not_completed"
    assert direct.raised.detail is None
    assert direct.raised.__cause__ is None
    # The sanitized failure must not retain the original, whose detail and
    # cause carry the credential, as its context.
    assert direct.raised.__context__ is None
    assert _SECRET not in "".join(traceback.format_exception(direct.raised))
    container = invoke_step_in_container(
        _binding(), {"mode": "terminal_failure_detail_leak"}, context=context
    )
    assert isinstance(container.raised, ContainerRemoteError)
    assert container.raised.code is FailureCode.REMOTE_REJECTED
    assert container.raised.message == "The tracker rejected the request."
    assert _SECRET not in repr(container)
    event = _terminal_failure_event(
        _binding(), {"mode": "terminal_failure_detail_leak"}, path, tmp_path
    )
    assert event.WhichOneof("event") == "failure"
    assert event.failure.code == "REMOTE_REJECTED"
    assert _SECRET not in str(event)


def test_a_mutated_terminal_failure_degrades_safely_after_a_read(
    tmp_path: Path,
) -> None:
    """A field mutated after construction cannot leak through reconstruction.

    The mutated provider code fails the reconstructed failure's validation;
    the safe degradation must not retain the original, whose cause carries
    the credential, as its context or formatted traceback.
    """

    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": _SECRET}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    direct = invoke_step(_binding(), {"mode": "terminal_failure_mutated"}, context=context)
    assert isinstance(direct.raised, StepExecutionError)
    assert direct.raised.failure_code is FailureCode.ABI_VIOLATION
    assert direct.raised.__context__ is None
    assert _SECRET not in str(direct.raised)
    assert _SECRET not in "".join(traceback.format_exception(direct.raised))
    container = invoke_step_in_container(
        _binding(), {"mode": "terminal_failure_mutated"}, context=context
    )
    assert isinstance(container.raised, ContainerRemoteError)
    assert container.raised.code is FailureCode.ABI_VIOLATION
    assert _SECRET not in repr(container)
    event = _terminal_failure_event(
        _binding(), {"mode": "terminal_failure_mutated"}, path, tmp_path
    )
    assert event.WhichOneof("event") == "failure"
    assert event.failure.code == "ABI_VIOLATION"
    assert _SECRET not in str(event)


def test_a_terminal_failure_message_cannot_leak_a_read_credential(tmp_path: Path) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": _SECRET}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    outcomes = (
        invoke_step(_binding(), {"mode": "terminal_failure_leak"}, context=context),
        invoke_step_in_container(
            _binding(), {"mode": "terminal_failure_leak"}, context=context
        ),
    )
    for outcome in outcomes:
        assert not outcome.ok
        assert outcome.raw is None
        code = (
            outcome.raised.failure_code
            if isinstance(outcome.raised, StepExecutionError)
            else outcome.raised.code
        )
        assert code is FailureCode.ABI_VIOLATION
        assert _SECRET not in repr(outcome)
        assert _SECRET not in str(outcome.raised)
        if isinstance(outcome.raised, StepExecutionError):
            assert outcome.raised.__context__ is None


def test_file_provider_refuses_unapproved_and_symlink_paths(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text(json.dumps({"token": _SECRET}))
    approved = tmp_path / "approved"
    approved.mkdir()
    link = approved / "link"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        FileCredentialProvider({_NAME: str(link)}, local_directory=approved)
    with pytest.raises(ValueError):
        FileCredentialProvider({_NAME: str(outside)})


@pytest.mark.parametrize(
    "mode,expected", [("scope_read", "undeclared"), ("provider_read", "undeclared"),
                      ("inner_provider_read", "missing"), ("rescope", "unauthorized")]
)
def test_undeclared_present_provider_entry_cannot_bypass_scope(
    tmp_path: Path, mode: str, expected: str
) -> None:
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    allowed.write_text(json.dumps({"token": _SECRET}))
    other.write_text(json.dumps({"token": "undeclared-secret"}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(allowed), _OTHER: str(other)},
        credential_directory=tmp_path,
    )
    for call in (invoke_step, invoke_step_in_container):
        outcome = call(_binding(), {"mode": mode}, context=context)
        assert outcome.ok
        assert outcome.output == {"result": expected}
        assert "undeclared-secret" not in repr(outcome)


def test_direct_scoped_provider_reads_are_tracked_and_revoked(tmp_path: Path) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": _SECRET}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    for call in (invoke_step, invoke_step_in_container):
        for mode in ("provider_allowed_leak", "inner_allowed_leak"):
            outcome = call(_binding(), {"mode": mode}, context=context)
            assert not outcome.ok
            assert outcome.raw is None
            assert _SECRET not in repr(outcome)
            with pytest.raises(CredentialAccessError, match="expired"):
                CredentialStep.saved_provider.read(_NAME)


@pytest.mark.parametrize("mode", ["number_leak", "boolean_leak", "key_leak", "number_failure", "boolean_failure", "status_code_leak"])
def test_stringified_scalar_and_mapping_key_secrets_do_not_reach_wrapper(
    tmp_path: Path, mode: str
) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"pin": 739251, "flag": True, "nested": {_KEY_SECRET: "value"}}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    for call in (invoke_step, invoke_step_in_container):
        outcome = call(_binding(), {"mode": mode}, context=context)
        assert not outcome.ok
        assert outcome.raw is None
        assert "739251" not in repr(outcome)
        assert _KEY_SECRET not in repr(outcome)


@pytest.mark.parametrize("mode", ["numeric_token_leak", "numeric_token_status_code_leak"])
def test_string_credentials_converted_to_numbers_do_not_reach_wrapper(
    tmp_path: Path, mode: str
) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"numeric_token": "8675309"}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    for call in (invoke_step, invoke_step_in_container):
        outcome = call(_binding(), {"mode": mode}, context=context)
        assert not outcome.ok
        assert outcome.raw is None
        assert "8675309" not in repr(outcome)


def test_zero_credential_allows_success_but_not_business_output(tmp_path: Path) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"pin": 0}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    for call in (invoke_step, invoke_step_in_container):
        outcome = call(_binding(), {"mode": "use"}, context=context)
        assert outcome.ok
        assert outcome.output == {"result": "accepted"}
        for mode in ("number_leak", "number_raw_leak"):
            outcome = call(_binding(), {"mode": mode}, context=context)
            assert not outcome.ok
            assert outcome.raw is None


@pytest.mark.parametrize("explicit", [False, True])
def test_execution_error_after_read_discards_exception_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": _SECRET}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )

    def fail_after_read(
        self: StepExecutor, inputs: Mapping[str, Any], scoped_context: ExecutionContext
    ) -> Any:
        token = scoped_context.credential(_NAME).read()["token"]
        try:
            raise ValueError(token)
        except ValueError as error:
            if explicit:
                raise StepExecutionError("safe failure", detail=token) from error
            raise StepExecutionError(token, detail=token)  # noqa: B904 - exercise implicit context

    monkeypatch.setattr(StepExecutor, "execute", fail_after_read)
    binding = _binding()
    with pytest.raises(StepExecutionError) as raised:
        invoke(binding, {"mode": "use"}, context)
    error = raised.value
    assert error.detail is None
    assert error.__cause__ is None
    assert error.__context__ is None
    assert _SECRET not in "".join(traceback.format_exception(error))


def test_execution_error_without_credential_read_keeps_debug_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_error = RuntimeError("useful failure detail")

    def fail_before_read(
        self: StepExecutor, inputs: Mapping[str, Any], context: ExecutionContext
    ) -> Any:
        raise source_error

    monkeypatch.setattr(StepExecutor, "execute", fail_before_read)
    binding = _binding()
    context = build_execution_context()
    with pytest.raises(StepExecutionError) as raised:
        invoke(binding, {"mode": "use"}, context)

    assert "useful failure detail" in (raised.value.detail or "")
    assert raised.value.__cause__ is source_error


@pytest.mark.parametrize("mode", ["system_exit", "keyboard_interrupt"])
def test_control_flow_exception_after_read_is_sanitized(tmp_path: Path, mode: str) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": _SECRET}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    binding = _binding()
    with pytest.raises(StepExecutionError) as raised:
        invoke(binding, {"mode": mode}, context)
    error = raised.value
    assert error.detail is None
    assert error.__cause__ is None
    assert error.__context__ is None
    assert _SECRET not in "".join(traceback.format_exception(error))

    outcome = invoke_step(binding, {"mode": mode}, context=context)
    assert not outcome.ok
    assert isinstance(outcome.raised, StepExecutionError)
    assert _SECRET not in repr(outcome)
    assert _SECRET not in "".join(traceback.format_exception(outcome.raised))


@pytest.mark.parametrize(
    "mode,error_type",
    [("system_exit_before", SystemExit), ("keyboard_interrupt_before", KeyboardInterrupt)],
)
def test_control_flow_exception_before_read_is_preserved(
    mode: str, error_type: type[BaseException]
) -> None:
    binding = _binding()
    context = build_execution_context()
    with pytest.raises(error_type, match="pre-read"):
        invoke(binding, {"mode": mode}, context)


def test_sdk_diagnostics_do_not_log_read_credentials(tmp_path: Path, caplog: Any) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": _SECRET}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    with caplog.at_level(logging.DEBUG):
        for call in (invoke_step, invoke_step_in_container):
            outcome = call(_binding(), {"mode": "exception"}, context=context)
            assert not outcome.ok
            assert outcome.raw is None
            assert _SECRET not in repr(outcome.raised)
            assert _SECRET not in str(getattr(outcome.raised, "detail", ""))
    assert _SECRET not in caplog.text


def test_fingerprint_scan_handles_long_values_and_fails_closed_on_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "s" * 20_000 + "private"

    scope = credentials_module._CredentialScope((_NAME,), _FakeProvider({"password": secret}))
    scope.read(_NAME)
    assert (
        scope.scan({"result": "x" * 200_000})
        is credentials_module._CredentialScanStatus.CLEAN
    )
    assert (
        scope.scan({"result": "prefix" + secret + "suffix"})
        is credentials_module._CredentialScanStatus.SECRET_FOUND
    )
    monkeypatch.setattr(credentials_module, "_MAX_SCAN_WORK", 100)
    assert scope.scan({"result": "x" * 200}) is credentials_module._CredentialScanStatus.LIMIT_EXCEEDED
    monkeypatch.setattr(credentials_module, "_MAX_OUTPUT_NODES", 5)
    assert (
        scope.scan({"result": [None] * 6})
        is credentials_module._CredentialScanStatus.LIMIT_EXCEEDED
    )
    scope.close()


def test_fingerprint_count_limit_is_reported_separately_from_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credentials_module, "_MAX_FINGERPRINTS", 2)
    scope = credentials_module._CredentialScope(
        (_NAME,), _FakeProvider({"alpha": "first", "beta": "second"})
    )
    scope.read(_NAME)
    assert (
        scope.scan({"result": "safe"})
        is credentials_module._CredentialScanStatus.LIMIT_EXCEEDED
    )
    assert scope.contains_secret({"result": "safe"})
    scope.close()


def test_large_credential_reports_scan_limit_in_direct_and_container_modes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credential"
    document = {"token": "safe-token"}
    document.update({f"field-{index}": f"value-{index}" for index in range(300)})
    path.write_text(json.dumps(document))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    for call in (invoke_step, invoke_step_in_container):
        outcome = call(_binding(), {"mode": "use"}, context=context)
        assert not outcome.ok
        assert outcome.raw is None
        failure = outcome.raised
        assert failure is not None
        code = getattr(failure, "failure_code", None) or getattr(failure, "code", None)
        assert code is FailureCode.CREDENTIAL_SCAN_LIMIT
        message = getattr(failure, "safe_message", None) or getattr(failure, "message", "")
        assert "reduce credential document size" in message


def test_large_step_output_reports_scan_limit_separately_from_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "credential"
    path.write_text(json.dumps({"token": "private-token"}))
    context = build_execution_context(
        credential_file_paths={_NAME: str(path)}, credential_directory=tmp_path,
    )
    monkeypatch.setattr(credentials_module, "_MAX_SCAN_WORK", 20)
    binding = _binding()
    with pytest.raises(StepExecutionError) as raised:
        invoke(binding, {"mode": "large_output"}, context)

    assert raised.value.failure_code is FailureCode.CREDENTIAL_SCAN_LIMIT
    assert "step output complexity" in raised.value.safe_message


def test_credential_free_invocations_do_not_apply_scan_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(credentials_module, "_MAX_OUTPUT_NODES", 2)
    scope = credentials_module._CredentialScope((), None)
    assert not scope.contains_secret({"result": [None] * 10})
    scope.close()


class _FakeView:
    def __init__(
        self, names: frozenset[str], document: dict[str, Any],
        on_read: Callable[[Mapping[str, Any]], None],
    ) -> None:
        self._names = names
        self._document = document
        self._on_read = on_read
        self._active = True

    def read(self, name: str) -> Mapping[str, Any]:
        if not self._active:
            raise CredentialAccessError("expired")
        if name not in self._names:
            raise CredentialAccessError("missing")
        self._on_read(self._document)
        return self._document

    def close(self) -> None:
        self._active = False


class _FakeProvider:
    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document

    def restrict(
        self, names: frozenset[str], on_read: Callable[[Mapping[str, Any]], None]
    ) -> _FakeView:
        return _FakeView(names, self._document, on_read)


def test_scope_tracks_each_provider_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original = credentials_module._CredentialScope._record

    def count_record(
        scope: credentials_module._CredentialScope, document: Mapping[str, Any]
    ) -> None:
        nonlocal calls
        calls += 1
        original(scope, document)

    monkeypatch.setattr(credentials_module._CredentialScope, "_record", count_record)
    scope = credentials_module._CredentialScope((_NAME,), _FakeProvider({"token": _SECRET}))
    scope.read(_NAME)
    assert calls == 1
    scope.close()


@pytest.mark.parametrize("mode", ["missing", "raises", "same"])
def test_unscopable_host_provider_is_denied_safely(mode: str) -> None:
    class MissingRestriction:
        def read(self, name: str) -> Mapping[str, Any]:
            raise AssertionError("unrestricted read must not run")

    class BrokenRestriction(MissingRestriction):
        def restrict(
            self, names: frozenset[str], on_read: Callable[[Mapping[str, Any]], None]
        ) -> Any:
            if mode == "raises":
                raise ValueError(_SECRET)
            return self

    provider: Any = MissingRestriction() if mode == "missing" else BrokenRestriction()
    context = ExecutionContext(invocation_id="denied", credential_provider=provider)
    outcome = invoke_step(_binding(), {"mode": "use"}, context=context)
    assert outcome.ok
    assert outcome.output == {"result": "unauthorized"}
    assert _SECRET not in repr(outcome)


def test_success_status_message_cannot_expose_credential() -> None:
    from syntara_plugin.runtime.execution import _reject_credential_output
    from syntara_plugin.runtime.outcome import StandardOutputWrapper

    scope = credentials_module._CredentialScope((_NAME,), _FakeProvider({"token": _SECRET}))
    try:
        scope.read(_NAME)
        wrapper = StandardOutputWrapper.success(
            {"result": "safe"}, status_message=_SECRET
        )
        with pytest.raises(StepExecutionError) as error:
            _reject_credential_output(wrapper, scope)
        assert _SECRET not in str(error.value)
    finally:
        scope.close()
