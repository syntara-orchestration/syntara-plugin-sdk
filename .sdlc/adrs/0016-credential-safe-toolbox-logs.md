# ADR 0016: Credential-safe Toolbox log delivery

- Status: Accepted
- Date: 2026-11-24
- Amends: [ADR 0013](0013-toolbox-logging.md)

## Decision

Screen queued Toolbox records at the SDK invocation boundary, in addition to
the host's screening before persistence or export. Drop entire records whose
severity, message, event name, or attribute keys/values match a credential fingerprint,
or whose scan fails or exceeds its budget. Increment the existing saturating
`dropped_count`; preserve safe records in order. Do not change the protobuf
layout, batch version, or runtime ABI.

The collector freezes once, including during cancellation, and scans against
credentials already read by that invocation. Normal scope teardown revokes
reads, finalizes logs while fingerprints are still available, then clears
them. A weak reference connects the collector to the scope: it must not keep
an abandoned scope or raw credential provider alive. For container
execution, scope teardown transfers only fingerprints and the remaining log
budget to a detached scanner, then clears the scope before terminal result
construction. An unavailable bound
scanner fails closed; a collector with no credential scope does not require a
scanner. Log scans share a separate invocation-wide budget of 10,000
nodes and 4,000,000 scan-work units, independent of wrapper output scanning.
Late credential reads therefore also cover records queued earlier.

Logging remains best effort. Collection, scan, or serialization failures must
not replace or prevent a prepared terminal result/failure. A batch containing
only a drop count remains a valid version-1 notification. A construction
failure may prevent the notification itself from being delivered.
Container cancellation, timeout, and an unexpected client message screen
queued records instead of discarding them. They first revoke credential reads
and detach the final fingerprints under the scope's record lock, so a
concurrent read is either fingerprinted first or fails as expired before its
document reaches step code. The records are then scanned within the remaining
invocation budget and a one-second wall-clock limit, off the asynchronous event
loop; the work budget bounds CPU, not latency, and the terminal event must stay
inside the host's delivery grace. Records are discarded and counted only when
another thread is already scanning them (cancellation never waits for that
scan), when revocation fails, when the scan does not finish within that limit
(the abandoned scan is stopped), or on runtime shutdown, which does not scan.
A read cut off by termination never records fingerprints, although its value
may already sit in a queued record, so the fingerprint set is treated as
incomplete and every record is dropped when a read is still in flight at
revocation or, once the invocation is cancelled or timed out, when any read
failed. A read still in flight at normal scope teardown has the same effect.

Each invocation commits exactly one terminal event. Every terminal path claims
it through one gate, and a result or handled failure prepared before the claim
is delivered instead of the cancellation or timeout failure. A step outcome
that arrives after another path claimed the terminal event is discarded, and
that step leaves log finalization to the claiming path. Fingerprints are
detached from the scope once; scope teardown and cancellation share that
detached scanner, and a closed scope without one fails closed. If
finalization outlasts the deadline after the terminal result is prepared,
unfinished records are dropped and the prepared result is retained.

## Security boundary

The scan uses the existing exact-value/substring credential fingerprints;
it does not prevent transformed-secret exfiltration, arbitrary console output,
or a deliberately modified runtime. Plugin authors must keep secrets out of
all logs. EP and AO retain responsibility for screening records they capture,
persist, or export; SDK filtering does not replace those controls.

This approved security hardening supersedes the earlier SDK non-inspection
decision. The handbook's ANSTRAT-2422 proposal still needs matching wording
for SDK pre-delivery screening alongside EP screening and best-effort batch
construction. Its older prohibition is not the implementation target for
this change.

## State cleanup investigation

The reported stale-ID/weakref-finalizer race is unproven. A Python 3.13 stress
check with 1,000 abandoned cyclic provider/view graphs, live collectors, and
repeated forced GC found no lost live state; registries returned to baseline.
The automated cyclic-GC regression also passed on Python 3.12.13, 3.13.15,
and stable 3.14.0 without reproducing the claimed collision.
Delayed cyclic collection alone does not establish address reuse before
finalization. Keep the registries' id-keyed ownership unchanged without a
reproducible collision; test cleanup and contain logging faults independently.
This check does not certify every supported interpreter or timing interleaving.

Two other cleanup defects were reproduced and fixed. The toolbox and
credential-callback registries had separate locks, and garbage collection can
run either registry's weakref finalizer on any thread while the other lock is
held, so concurrent invocations deadlocked under churn; both registries now
share one reentrant lock. The asynchronous runtime's gRPC done callback held
the invocation's provider strongly, and grpc.aio keeps that callback reachable
after the RPC, so every invocation's provider and log state stayed alive; the
callback now holds the provider weakly. Each fix has a regression test that
fails without it.

## Compatibility

No wire fields, versions, request shapes, or terminal result semantics change.
Hosts can observe fewer log records and a larger drop count. Current EP
forwarding accepts drop-count-only batches and its warning is generic; its
overflow-focused comments and documentation may need broader wording. Runtime
images must be rebuilt to acquire the protection.
