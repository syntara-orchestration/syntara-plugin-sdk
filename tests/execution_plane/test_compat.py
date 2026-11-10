"""Run inside the pinned EP integration suite against published step images."""

from __future__ import annotations

import os
import uuid

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
    ("image_env", "step", "inputs", "expected"),
    [
        (
            "EP_COMPAT_PYTHON_IMAGE",
            "syntara.script-python.script-python",
            {"code": "import json\nprint(json.dumps({'result': 42}))"},
            {"return_code": 0, "stdout_json": {"result": 42}},
        ),
        (
            "EP_COMPAT_BASH_IMAGE",
            "syntara.script-bash.script-bash",
            {"code": "printf 'hello from bash\\n'"},
            {"return_code": 0, "stdout": "hello from bash\n"},
        ),
        (
            "EP_COMPAT_HTTP_IMAGE",
            "syntara.http-request.http-request",
            {"url": "http://100.64.0.100/result.json", "method": "GET"},
            {"status_code": 200, "body": {"fixture": "execution-plane-compat", "value": 42}},
        ),
    ],
)
async def test_published_step_dispatch(
    ep_cluster, integration_credential_resolver, image_env, step, inputs, expected
):
    database_url = os.environ["EP_TEST_DATABASE_URL"]
    settings = get_settings()
    client_id = "ep-it-dispatch"
    payload = {
        "image": os.environ[image_env],
        "selectors": {},
        "invocation": {
            "workflow_run_id": str(uuid.uuid4()),
            "step_revision_id": step,
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
    assert item.status is WorkItemStatus.COMPLETED, f"{step}: {item.status}; result={item.result}"
    assert item.result is not None
    assert item.result["status_code"] == 0
    for key, value in expected.items():
        assert item.result["output"][key] == value
