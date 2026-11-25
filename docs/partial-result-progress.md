# Partial-result progress events

A long-running custom-runner step can publish structured snapshots of useful
partial output while it continues working. Call
`ExecutionContext.report_partial_result()` from the step:

```python
context.report_partial_result({"job_id": "123", "job_url": "https://aap.example/jobs/123"})

# Publish a fresh partial view and mark it as the last update.
context.report_partial_result({
    "job_id": "123",
    "job_url": "https://aap.example/jobs/123",
    "status": "running",
}, is_last=True)

# If no partial data is available, use this instead:
# context.report_partial_result({}, is_last=True)
```

Each call emits a non-terminal `ContainerProgress` event with
`state: "progress"` and an optional `partialResult` object containing a
boolean `isLast` and JSON object `data`. Each `data` value is a fresh partial
view. Consumers replace the previous partial view with the new one; omitted
fields are not carried forward. A partial view may omit fields required by the
final output. Sequence numbers order updates. `isLast: true` tells the host
that no more partial updates will follow. After reporting it, a step cannot
report another partial result. The `message` and `percent` fields remain
optional. Partial results do not complete the step or replace its final output.

The container runtime checks each partial view against the step's final output
schema. Object properties may be omitted, while every supplied property and
value must conform to that schema. For conditional output schemas, include the
branch discriminator when sending branch-specific fields. Arrays supplied in a
partial view must satisfy their complete array schema. The final output is
still validated against the unmodified output schema. Each `data` object may
be empty and can have at most 32 top-level properties; its canonical UTF-8
encoding must be no larger than 16 KiB. If a step has no partial data, send
`{}` with `is_last=True` to let the host stop waiting for updates. Do not
include credentials or other secrets. Local SDK progress callbacks recursively
scrub known input and credential values.

For example, a step with no partial data can send this final progress event:

```json
{
  "apiVersion": "syntara.io/v1alpha1",
  "kind": "ContainerProgress",
  "invocationId": "invoke-42",
  "sequence": 1,
  "state": "progress",
  "partialResult": {
    "isLast": true,
    "data": {}
  }
}
```

Delivery is best-effort: a host may miss, duplicate, or receive a snapshot
after terminal completion. The container ABI is `syntara.container/v1alpha2`;
the host must support this version to consume and forward partial results. See
the [runtime and OCI model](runtime-and-oci.md#partial-result-progress-events)
for transport details and [ADR 0015](../.sdlc/adrs/0015-partial-result-progress.md)
for the design decision.
