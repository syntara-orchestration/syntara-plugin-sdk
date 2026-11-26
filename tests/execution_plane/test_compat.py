"""Run the checkout's registered plugin through the pinned EP Kubernetes worker."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from execution_plane.cluster.cluster_store import ClusterStore
from execution_plane.config.settings import get_settings
from execution_plane.credential_resolution.errors import CredentialResolutionError
from execution_plane.credential_resolution.workload import WorkloadCredentialResolver
from execution_plane.execution_target.execution_target_store import ExecutionTargetStore
from execution_plane.models.work_item import WorkItemStatus
from syntara_plugin.contracts import canonical_json_bytes
from tests.integration.kind.test_dispatch import (
    _dispatch_item,
    _process_until_terminal,
    _work_service,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

CREDENTIAL_RECIPE_ID = "compat.credentials.bearer-token"
_LOOPBACK_URL = "http://127.0.0.1:18080/verify"
_DIGEST_LINE = "COMPAT_CREDENTIAL_SHA256="


class CompatWorkloadCredentialResolver(WorkloadCredentialResolver):
    """Answer only the compat recipe for the dispatching work item.

    The AO-free stand-in for AO's ``credential.resolve.workload`` command:
    it holds one synthetic document and refuses every unexpected request, so
    a workload asking for anything but the bound recipe fails closed.
    """

    def __init__(
        self, recipe_id: str, credential_ref: uuid.UUID, document: Mapping[str, str]
    ) -> None:
        """Keep the one approved binding and its synthetic document."""
        self._recipe_id = recipe_id
        self._credential_ref = credential_ref
        self._document = dict(document)

    async def resolve_workload(
        self,
        credential_ref: uuid.UUID,
        credential_recipe_id: str,
        *,
        work_item_id: uuid.UUID,
        attempt_id: str,
        step_revision_id: str,
        runtime_context: Mapping[str, object] | None = None,
    ) -> dict[str, str]:
        """Return the synthetic document for the approved binding only.

        Execution Plane passes the work item's ``runtime_context`` for
        loop-aware correlation; it carries no authorization, so the stand-in
        accepts it and ignores it. The default keeps older ``ep-ref``
        revisions, which do not send it, working.
        """
        if (
            credential_recipe_id != self._recipe_id
            or credential_ref != self._credential_ref
            or work_item_id == uuid.UUID(int=0)
        ):
            message = "Compat workload credential resolution refused an unexpected request"
            raise CredentialResolutionError(message)
        return dict(self._document)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "inputs", "expected"),
    [
        ("example.existing-step.create-ticket", {"title": "compat"}, {"ticket_id": ":compat"}),
        (
            "example.existing-step.normalize-records",
            {"records": ["Alpha", "Beta"]},
            {"count": 2, "normalized": ["alpha", "beta"]},
        ),
        (
            "example.existing-step.record-opened",
            {"repository": "sdk"},
            {"repository": "sdk"},
        ),
    ],
)
async def test_checkout_plugin_dispatch(
    ep_cluster,
    integration_credential_resolver,
    integration_workload_credential_resolver,
    identity,
    inputs,
    expected,
):
    manifest = json.loads(Path(os.environ["EP_COMPAT_REGISTRATION"]).read_text(encoding="utf-8"))
    revision, entry = next(
        (revision, entry)
        for revision, entry in manifest["steps"].items()
        if entry["stepIdentity"] == identity
    )
    image = os.environ["EP_COMPAT_IMAGE"]
    assert "@sha256:" in image
    database_url = os.environ["EP_TEST_DATABASE_URL"]
    settings = get_settings()
    client_id = "ep-it-sdk-compat"
    execution_id = str(uuid.uuid4())
    payload = {
        "image": image,
        "selectors": {},
        "runtime_context": {"execution_id": execution_id},
        "invocation": {
            "step_revision_id": revision,
            "inputs": inputs,
            "timeout_seconds": 120,
        },
    }
    async with (
        ClusterStore.from_database_url(database_url) as cluster_store,
        ExecutionTargetStore.from_database_url(database_url) as target_store,
    ):
        async with _work_service(
            database_url,
            cluster_store,
            target_store,
            settings,
            integration_credential_resolver,
            integration_workload_credential_resolver,
        ) as work:
            dispatched = await _dispatch_item(
                work, work.worker_managers, client_id=client_id, payload=payload
            )
            item = await _process_until_terminal(
                dispatched.assigned, work, target_store, settings, client_id=client_id
            )
    assert item is not None
    assert item.status is WorkItemStatus.COMPLETED, (
        f"{identity}: {item.status}; result={item.result}"
    )
    assert item.result is not None
    assert item.result["status_code"] == 0
    assert item.result["output_schema_digest"] == entry["schemaDigests"]["output"]
    if identity.endswith("create-ticket"):
        assert item.result["output"]["ticket_id"].endswith(expected["ticket_id"])
        assert item.result["output"]["ticket_id"] != expected["ticket_id"]
    elif identity.endswith("record-opened"):
        assert item.result["output"] == {**expected, "event_id": f"{execution_id}:sdk"}
    else:
        assert item.result["output"] == expected


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "inputs", "marker"),
    [
        (
            "compat.credential-steps.script-bash",
            {"code": "echo compat-cred-bash", "environment": {"COMPAT_USE_CREDENTIAL": "1"}},
            "compat-cred-bash",
        ),
        (
            "compat.credential-steps.script-python",
            {
                "code": "print('compat-cred-python')",
                "environment": {"COMPAT_USE_CREDENTIAL": "1"},
            },
            "compat-cred-python",
        ),
        (
            "compat.credential-steps.http-request",
            {"url": _LOOPBACK_URL, "method": "GET"},
            None,
        ),
    ],
)
async def test_checkout_credential_plugin_dispatch(
    ep_cluster,
    integration_credential_resolver,
    identity,
    inputs,
    marker,
):
    """Dispatch one credential step shape and prove the credential was used."""
    manifest = json.loads(
        Path(os.environ["EP_COMPAT_CREDENTIAL_REGISTRATION"]).read_text(encoding="utf-8")
    )
    revision, entry = next(
        (revision, entry)
        for revision, entry in manifest["steps"].items()
        if entry["stepIdentity"] == identity
    )
    image = os.environ["EP_COMPAT_CREDENTIAL_IMAGE"]
    assert "@sha256:" in image
    token = os.environ["EP_COMPAT_CREDENTIAL_TOKEN"]
    document = {"auth_type": "bearer", "bearer_token": token}
    expected_digest = hashlib.sha256(canonical_json_bytes(document)).hexdigest()
    credential_ref = uuid.uuid4()
    database_url = os.environ["EP_TEST_DATABASE_URL"]
    settings = get_settings()
    client_id = "ep-it-sdk-compat-cred"
    execution_id = str(uuid.uuid4())
    payload = {
        "image": image,
        "selectors": {},
        "runtime_context": {"execution_id": execution_id},
        "invocation": {
            "step_revision_id": revision,
            "inputs": inputs,
            "timeout_seconds": 120,
            "credential_bindings": [
                {
                    "credential_recipe_id": CREDENTIAL_RECIPE_ID,
                    "credential_ref": str(credential_ref),
                }
            ],
        },
    }
    resolver = CompatWorkloadCredentialResolver(CREDENTIAL_RECIPE_ID, credential_ref, document)
    async with (
        ClusterStore.from_database_url(database_url) as cluster_store,
        ExecutionTargetStore.from_database_url(database_url) as target_store,
    ):
        async with _work_service(
            database_url,
            cluster_store,
            target_store,
            settings,
            integration_credential_resolver,
            resolver,
        ) as work:
            dispatched = await _dispatch_item(
                work, work.worker_managers, client_id=client_id, payload=payload
            )
            item = await _process_until_terminal(
                dispatched.assigned, work, target_store, settings, client_id=client_id
            )
    assert item is not None
    # Fail with a static message before any assertion whose failure message
    # renders the result: a leak must never echo the token into the JUnit
    # report, the uploaded diagnostics, or local output.
    if token in json.dumps(item.result):
        pytest.fail("The credential token appeared in the persisted work-item result")
    assert item.status is WorkItemStatus.COMPLETED, (
        f"{identity}: {item.status}; result={item.result}"
    )
    assert item.result is not None
    assert item.result["status_code"] == 0
    assert item.result["output_schema_digest"] == entry["schemaDigests"]["output"]
    output = item.result["output"]
    if marker is not None:
        assert output["return_code"] == 0
        assert output["stdout"] == f"{marker}\n{_DIGEST_LINE}{expected_digest}\n"
    else:
        assert output["status_code"] == 200
        assert output["body"] == {
            "authenticated": True,
            "token_sha256": expected_digest,
        }


@pytest.mark.asyncio
async def test_compat_workload_credential_resolver_refuses_unexpected_requests() -> None:
    """The AO-free resolver answers only the approved binding."""
    document = {"auth_type": "bearer", "bearer_token": "synthetic-resolver-secret-0123456789"}
    credential_ref = uuid.uuid4()
    resolver = CompatWorkloadCredentialResolver(CREDENTIAL_RECIPE_ID, credential_ref, document)

    approved = await resolver.resolve_workload(
        credential_ref,
        CREDENTIAL_RECIPE_ID,
        work_item_id=uuid.uuid4(),
        attempt_id="attempt",
        step_revision_id="revision",
    )
    assert approved == document

    with pytest.raises(CredentialResolutionError):
        await resolver.resolve_workload(
            uuid.uuid4(),
            CREDENTIAL_RECIPE_ID,
            work_item_id=uuid.uuid4(),
            attempt_id="attempt",
            step_revision_id="revision",
        )
    with pytest.raises(CredentialResolutionError):
        await resolver.resolve_workload(
            credential_ref,
            "compat.credentials.other-recipe",
            work_item_id=uuid.uuid4(),
            attempt_id="attempt",
            step_revision_id="revision",
        )
    with pytest.raises(CredentialResolutionError):
        await resolver.resolve_workload(
            credential_ref,
            CREDENTIAL_RECIPE_ID,
            work_item_id=uuid.UUID(int=0),
            attempt_id="attempt",
            step_revision_id="revision",
        )
