# ADR 0007: SDK local test harness

- Status: Accepted (the package-layout decision is superseded by ADR 0008;
  the remaining decisions still stand)
- Date: 2026-10-19

## Context

Step authors need a fast local feedback loop for declared contracts and the
Python API: manifest validation, model-versus-manifest drift, and execution of
Action, Task, Workflow, and Trigger implementations through the shared SDK
executor, without AO, EP, or a cluster. Before this decision the repository had
no shared test fixtures (roughly ten test files duplicated private helpers),
no entry-point loading (`spec.runtime.entryPoint` was validated but never
imported by live code), and only one example, an http platform-runner Action
with no Python code.

External research (Home Assistant, Airflow, Terraform, Salt, VS Code, Grafana,
Backstage, Prefect, Dagster, controller-runtime envtest, the pytest plugin
ecosystem) points to a small set of patterns: ship the harness as a separate
package, keep a function core under thin pytest fixtures, run the real
platform code paths in-process, treat fixture names as public API, and avoid
Docker, autouse fixtures, lockstep pins, and white-box assertions.

## Decision

1. Add a fourth workspace package, `syntara-plugin-testing`
   (`syntara_plugin.testing`), depending on `pytest` and the three existing
   packages. It sits above runtime and sdk and preserves the rule that runtime
   does not depend on sdk.
2. Layer it as a function core with a fixture shell: every capability is an
   importable, framework-agnostic function; the `pytest11` entry point
   distributes exactly four fixtures wrapping that core
   (`manifest_validator`, `compile_manifest`, `mock_execution_context`,
   `step_bindings`). Fixture names are public API with a deprecation policy.
   There are no autouse fixtures and no required pytest configuration.
3. Replicate, never reimplement, platform behavior: invocation helpers route
   through `StepExecutor`, `validate_step_inputs`, `StepBindings`, and
   `load_bundle()`. Manifest checks use the canonical schema bundle or an
   owner-supplied bundle. Action bindings are built only through
   `compile_workspace` and `InstalledActionManifest.from_manifest`, so the
   SDK's provenance gate enforces the no-fabricated-identity rule.
4. Normalize outcomes behind a `StepOutcome` accessor so result assertions
   never touch raw envelope fields; direct and container-adapter invocation
   both produce it, and `assert_outcomes_equivalent` compares the public
   contract while ignoring legitimately differing schema digests and mapping
   executor exceptions to container failure events.
5. Ship the ticket's seven payload categories as data plus reference steps,
   with sentinel-based secret-leak assertions.
6. Scaffold tests from the compiled workspace through a new
   `syntara-plugin test-init` CLI command. The generator lives in the sdk
   package and emits text importing the testing package, so no dependency
   cycle is introduced; generation is deterministic and refuses silent
   overwrites.
7. Support sequential chaining with explicit seams (`chain_steps`) and a
   static seam check (`check_seam_compatible`, reusing the drift engine), and
   document that the harness is deliberately not a workflow engine.

## Consequences

- Authors get fixtures on install with zero conftest wiring; the repository's
  own suite dogfoods them, and `pytester` verifies the entry-point plugin
  against isolated fresh projects.
- The equivalence claim is scoped honestly: the harness proves the SDK's own
  direct and container paths agree, which is the successor sequence AO will
  adopt; it does not replicate AO's current old-SDK container path.
- Envelope changes (for example the in-flight immutable-envelope work) remain
  a one-file adapter change inside `invocation.py` because assertions go
  through `StepOutcome`.
- Existing duplicated test helpers are intentionally not migrated in this
  story; consolidation is a follow-up.
- The container helper starts one process-local gRPC server per invocation.
  In-process startup is milliseconds; if profiles ever justify it, server
  reuse can be added behind the same function signature.
