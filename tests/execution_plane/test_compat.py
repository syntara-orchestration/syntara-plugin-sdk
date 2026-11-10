"""Run the checkout's registered plugin through the pinned EP Kubernetes worker."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from execution_plane.cluster.cluster_store import ClusterStore
from execution_plane.config.settings import get_settings
from execution_plane.execution_target.execution_target_store import ExecutionTargetStore
from execution_plane.models.work_item import WorkItemStatus
from tests.integration.kind.test_dispatch import (
    _dispatch_item,
    _process_until_terminal,
    _work_service,
)


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
    ep_cluster, integration_credential_resolver, identity, inputs, expected
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
    workflow_run_id = str(uuid.uuid4())
    payload = {
        "image": image,
        "selectors": {},
        "invocation": {
            "workflow_run_id": workflow_run_id,
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
            database_url, cluster_store, target_store, settings, integration_credential_resolver
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
        assert item.result["output"] == {**expected, "event_id": f"{workflow_run_id}:sdk"}
    else:
        assert item.result["output"] == expected
