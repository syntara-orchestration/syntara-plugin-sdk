# Subworkflow composition example

This workspace is the local confirmation evidence for subworkflow composition
step types (AAP-95476 / ANSTRAT-2347). It shows that both sides of a
parent-child workflow call are expressible with the existing `v1alpha1`
contracts, with no new Step category and no schema change.

| Target | Kind | Role |
| --- | --- | --- |
| `steps/call-publish-artifact` | `Control` | Parent-side `subworkflow_call`. |
| `steps/publish-artifact-requested` | `Trigger` | Child-side `subworkflow_trigger`. |
| `steps/publish-artifact` | `Action` | Child workflow's terminal step. |

## The parent here is a stand-in

The contract-true parent declares the platform runner that performs the
dispatch:

```yaml
runtime:
  kind: platform-runner
  implementation: syntara.workflow.invoke@1.0.0
```

That shape is pinned in
[`contracts/fixtures/subworkflow-call.valid.json`](../../contracts/fixtures/subworkflow-call.valid.json)
and validated by the contract tests. The platform owns that runner, so it
cannot run locally: only custom-runner targets execute locally, by design.

This workspace therefore declares the parent as a `custom-runner` Control with
the same input and output contracts. That makes the composition seams testable
offline, but it proves the schema and envelope wiring only. It does **not**
prove platform dispatch, workflow version resolution, or Used-By guards.

The child side is a stand-in in its `runtime` too. A `Trigger` declares the
platform driver in `spec.driver`, but the contract-true executor is the
platform runner pinned in
[`subworkflow-trigger.valid.json`](../../contracts/fixtures/subworkflow-trigger.valid.json)
(`syntara.subworkflow.event@1.0.0`); this workspace declares a `custom-runner`
executor instead, for the same local-execution reason as the parent. Only
`spec.driver: subworkflow.v1` and the input/output contracts are shared with
the fixture; the runtime is not.

## Run it

```sh
uv run --no-sync pytest examples/subworkflow-composition -q
```

The tests validate all three manifests against the canonical contract bundle,
execute each step on the direct and container paths, prove the trigger's event
payload satisfies the terminal step's declared input, and chain the child's
steps to show the parent receives exactly the result it declares.
