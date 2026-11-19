# ANSTRAT-2347 node-sdk subworkflow POC — runbook

**Verdict:** Ship AO V1 engine-native. Use the SDK for catalog/contract
language and a future platform-runner. Do **not** replace Temporal child
dispatch with a custom-runner plugin.

## Phases

| Phase | Where | Proves |
| --- | --- | --- |
| 1 Contract fit | This example (`docs/ao-contract-mapping.md`, `fixtures/ao/`, `tests/test_ao_contract_mapping.py`) | Field matrix + three bridge gaps |
| 2 Local composition | This example (`scripts/run_poc.sh`, `ao_envelope.py`) | Parent→trigger→terminal runs offline; AO envelope wrap |
| 3 AO bridge | Syntara `backend/experiments/node_sdk_subworkflow_bridge/` | Control/Trigger → `SubWorkflowExecutorParameters` with injections |

## Commands

### Phase 1–2 (syntara-node-sdk)

```sh
cd /path/to/syntara-node-sdk
make install
bash examples/subworkflow-composition/scripts/run_poc.sh
```

Or separately:

```sh
uv run --no-sync pytest examples/subworkflow-composition -q
```

### Phase 3 (syntara)

```sh
cd /path/to/syntara/backend
PYTHONPATH=src:experiments .venv/bin/python -m pytest experiments/node_sdk_subworkflow_bridge -q
```

## Go / no-go

| Question | Answer |
| --- | --- |
| Can V1 ship without node-sdk schema changes? | **Go** — Rohit confirmed; POC agrees |
| Can a custom-runner replace AO child dispatch? | **No-go** — ABI has no workflow-spawn channel |
| Should contracts add `trigger_node_id` / UUID workflow now? | **Defer** — inject in control plane until EP catalog installs Controls |
| Useful SDK follow-ups? | Keep seam helpers + docs; later implement platform-runners that call AO |

## Gap summary (must inject at bridge)

1. `workflow_id` (UUID) ← not `qualifiedIdentifier`
2. `trigger_node_id` ← absent on `spec.invocation`
3. Output envelope ← AO metadata vs SDK business result
