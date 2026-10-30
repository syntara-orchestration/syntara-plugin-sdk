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
the latest published version of `example.subworkflow-composition.publish-artifact`
at invocation time.

## Starting the child workflow

**Publish artifact requested** is the child workflow's trigger. It receives the
artifact and channel from the parent and emits them together with the parent's
run identifier, so the child workflow can record which parent invoked it.
