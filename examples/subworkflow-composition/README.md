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
  driver: workflow.v1
```

That shape is pinned in
[`contracts/fixtures/subworkflow-call.valid.json`](../../contracts/fixtures/subworkflow-call.valid.json)
and validated by the contract tests. The platform owns `workflow.v1`, so it
cannot run locally: only custom-runner targets execute locally, by design.

This workspace therefore declares the parent as a `custom-runner` Control with
the same input and output contracts. That makes the composition seams testable
offline, but it proves the schema and envelope wiring only. It does **not**
prove platform dispatch, workflow version resolution, or Used-By guards.

The child side needs no such substitution. A `Trigger` declares the platform
driver in `spec.driver` and its executor separately in `spec.runtime`, so it is
contract-true (`subworkflow.v1`) and locally runnable at the same time.

## Run it

```sh
uv run --no-sync pytest examples/subworkflow-composition -q
```

The tests validate all three manifests against the canonical contract bundle,
execute each step on the direct and container paths, prove the trigger's event
payload satisfies the terminal step's declared input, and chain the child's
steps to show the parent receives exactly the result it declares.
