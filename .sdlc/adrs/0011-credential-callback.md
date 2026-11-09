# ADR 0011: Invocation-scoped credential callbacks

- Status: Accepted
- Date: 2026-11-09
- Supersedes: [ADR 0009](0009-local-credential-delivery.md)

## Decision

The `syntara.container/v1alpha2` ABI uses a bidirectional Execute stream.
Its first client message starts the invocation. The workload sends a credential
request with its invocation ID, a fresh request ID, and a logical name: the
CredentialRecipe ID declared by the installed step's integration. The host
resolves that name using its authority for this attempt and responds with the
same IDs and either a JSON object or a safe error category. Credential IDs,
references, paths, and provider error details do not cross this boundary.
Sensitive responses are separate from business events. The production channel
must authenticate both peers and provide confidentiality, using mTLS.

The generic `ContainerWorkloadRequest` no longer carries file-based credential
leases. Its existing opaque work-item credential handles remain platform-side;
the callback protocol does not send them to the workload or prescribe their
mapping to requested names.

The runtime accepts at most 16 concurrent reads, bounds documents to 1 MiB,
and limits each read to 30 seconds and the invocation deadline. Scope closure,
cancellation, deadline, disconnect, and shutdown revoke pending reads. Late,
duplicate, and mismatched responses are discarded. Successful reads are recorded
before the step can observe them so the existing bounded output scanner can
reject leaks. An access failure never includes provider details. Hosts must not
log sensitive messages. The callback grants access only for the invocation;
it is not a sandbox for arbitrary Python code.

The local CLI serves supplied credentials from memory over a private socket in
source mode or an authenticated channel to its managed image. It writes no
delivery files and keeps rejecting credentials for self-managed runtimes.

## Consequences

The ABI is intentionally forward-only and old images must be rebuilt and
registered with the new ABI. The Execution Plane must implement the matching
stream and per-attempt name-to-credential mapping. The SDK does not call the
credential authority or perform that mapping itself.
