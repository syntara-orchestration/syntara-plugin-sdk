# AO ↔ node-sdk contract mapping (ANSTRAT-2347)

Field-level comparison between Automation Orchestrator's in-tree sub-workflow
shapes and the SDK `v1alpha1` Control / Trigger composition contracts.

Reference fixtures under [`../fixtures/ao/`](../fixtures/ao/) snapshot the AO
parameter and output schemas. SDK fixtures live in
[`../../../contracts/fixtures/`](../../../contracts/fixtures/)
(`subworkflow-call.valid.json`, `subworkflow-trigger.valid.json`).

## Parent call

| Concern | AO (`sub_workflow`) | SDK (`Control` + `workflow-invocation`) | Fit |
| --- | --- | --- | --- |
| Category / kind | Engine node `type: sub_workflow` | `kind: Control` | Conceptual match; different catalogs |
| Child identity | `parameters.workflow_id` (UUID) | `spec.invocation.workflow` (`qualifiedIdentifier`) | **Gap** — needs control-plane mapping |
| Trigger selection | `parameters.trigger_node_id` (required) | No field on `spec.invocation` | **Gap** — inject at bridge |
| Inputs | `parameters.input_mapping` (KV / templates) | `spec.input` JSON Schema (declared shape) | Partial — schema vs values |
| Runtime owner | `SubWorkflowSetupActivity` + Temporal child | `platform-runner` `syntara.workflow.invoke@1.0.0` | Platform-owned; not local |
| Parent result | Execution metadata (`child_execution_id`, status, step-keyed `outputs`) | Child **business** result in `spec.output` | **Gap** — different envelopes |
| Version pin | Always latest published (control plane) | Version-less identifier (control plane) | Aligned intent |

## Child entry

| Concern | AO (`subworkflow_trigger`) | SDK (`Trigger` + `subworkflow.v1`) | Fit |
| --- | --- | --- | --- |
| Role | Eligibility + entry for Reference mode | `kind: Trigger`, `spec.driver: subworkflow.v1` | Conceptual match |
| Parameter schema | `parameters.input_schema` (per workflow) | `spec.configuration` + mirrored `spec.input` | Aligned pattern |
| Pass-through | Trigger result = invocation input | Event payload = input | Aligned |
| Runtime | In-tree `subworkflow_trigger` activity | `platform-runner` `syntara.subworkflow.event@1.0.0` | Platform-owned |

## Secondary registration gaps (platform catalog only)

These block **installing** composition step types via EP registration; they do
not block AO's current in-tree V1 path:

1. `TriggerDriver.spec.transport` has no parent-workflow value
2. `InstalledIdentity` excludes `control`
3. No approved-driver registry entry for `workflow.v1` / `subworkflow.v1`

## What this POC asserts

- Composition needs **no new Step category**.
- A bridge can map SDK Control/Trigger documents onto AO parameters only when
  the caller supplies `workflow_id` and `trigger_node_id`.
- Output alignment is a product decision (business result vs execution
  metadata); the bridge can wrap a business result into an AO-shaped envelope
  for demonstration without changing either contract.
