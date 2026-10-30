# Subworkflow composition

This plugin provides the two sides of a synchronous subworkflow call, plus the
child workflow's terminal step.

| Step type | Category | Purpose |
| --- | --- | --- |
| Call publish artifact | Control | Invokes the `publish-artifact` child workflow and returns its result. |
| Publish artifact requested | Trigger | Starts the child workflow when a parent workflow invokes it. |
| Publish artifact | Action | The child workflow's terminal step; produces the result the parent reads. |

## Calling the child workflow

Add **Call publish artifact** to a parent workflow and supply the artifact name
and the release channel. The step waits for the child workflow to finish and
returns its `releaseId` and `publishedAt` values, so later steps in the
parent workflow can read them directly.

The child workflow is selected by name, not by version. The platform resolves
the latest published version of `example.release.publish-artifact`
at invocation time. Workflow identities are platform-authored and live in
their own namespace (`example.release`), so they cannot be mistaken for a
plugin target identity like `example.subworkflow-composition.publish-artifact`.

## Starting the child workflow

**Publish artifact requested** is the child workflow's trigger. It receives the
artifact and channel from the parent and emits them as the child workflow's
first event. The event carries no parent-run identifier: `ExecutionContext`
has no parent-run field, so parent/child run correlation is a runtime-context
gap (AAP-94805) owned by the execution plane.
