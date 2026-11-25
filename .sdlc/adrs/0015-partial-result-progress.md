# ADR 0015: Partial-result progress data

- Status: Accepted
- Date: 2026-11-24

## Decision

A container step may attach a structured partial-result snapshot to a progress
event while it continues running. The event retains `state: progress` and adds
an optional `partialResult` object containing a boolean `isLast` and JSON
object `data`. Each update is a fresh partial view that replaces the previous
one and is ordered by the invocation's progress sequence. Fields omitted from
a later update are not carried forward. Setting `isLast: true` marks the final
partial-result update. The Python step API is
`ExecutionContext.report_partial_result(data, is_last=False)`; the client model
exposes the flag and data as a `ContainerPartialResult`.

Each `data` value is a JSON object with at most 32 top-level members and a
canonical encoding limited to 16384 bytes. The runtime validates partial data
against the declared output schema while allowing object properties to be
omitted; supplied properties and values must conform, and supplied arrays must
meet their complete array schema. Conditional schemas require the branch
discriminator alongside any branch-specific fields. Empty objects are allowed,
including for an `isLast: true` marker when no partial data was available.
After the final marker,
the SDK step API rejects another partial-result report. Partial data does not
end an invocation or replace the step's declared output. The terminal output
is validated against the unmodified schema. Reporting does not wait for host
acknowledgement. The SDK client rejects
partial-result progress after its final marker and all progress after a terminal
event. Execution Plane callback delivery is best-effort; downstream consumers
must tolerate missing, duplicate, or late delivery. The Execution Plane owns
correlation and forwarding to Syntara; this ABI does not define a Syntara
callback envelope.

The container ABI advances to `syntara.container/v1alpha2`. The partial-result
structure requires host support to expose its contents and final-update marker,
so the ABI version makes support explicit and requires the host and workload
to upgrade together. Local SDK progress callbacks recursively scrub known input
and credential values from the structured data.

## Consequences

Step authors can expose useful output snapshots while long-running work
continues. Consumers cannot assume they will receive every snapshot, receive
each once, or receive them before terminal completion. Partial-result payloads
are not credential delivery and must not contain secrets. Workloads and
Execution Plane clients must both implement the `v1alpha2` ABI before using the
feature.
