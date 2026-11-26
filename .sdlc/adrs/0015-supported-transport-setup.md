# ADR 0015: Supported runtime transport setup

- Status: Accepted
- Date: 2026-11-25

## Context

A ProdSec architecture review (AAP-102268) found that
`add_container_runtime_service` attaches the container runtime service to a
caller-owned gRPC server and does not control that server's listeners, while
nothing in the SDK documented the difference between service registration and
transport setup (AAP-103446). The hardened image path
(`create_runtime_server`/`serve_runtime`) already rejects TCP without mutual
TLS, but an integrator needing a synchronous server had to hand-roll
`grpc.server` plus registration plus `add_insecure_port`, which silently
permits plaintext TCP.

## Decision

1. The hardened async path (`serve_runtime`, `create_runtime_server`) is the
   documented production entry point for workload images.
2. `add_container_runtime_service` stays, documented as a low-level
   registration API: the caller owns every listener's authentication and
   lifecycle, and registration cannot reliably detect or restrict ports the
   server already has or will later add.
3. A new additive helper, `create_container_runtime_server`, owns synchronous
   listener construction for host processes: complete mutual-TLS material is
   required on TCP (clients must present a CA-signed certificate), and a
   private Unix-domain socket is the supported local IPC transport. It checks
   bind failure and stops the server and shuts the service down before
   raising, so a failed construction leaves nothing behind. It also disables
   gRPC's default `SO_REUSEPORT`, so a second server cannot silently share
   its address; the async image path keeps the gRPC default (pre-existing
   behavior, tracked as a follow-up). A TCP port of `0` binds an ephemeral
   port whose bound address is returned, and a Unix listener requires an
   absolute socket path that does not already exist: gRPC would otherwise
   unlink and silently replace a live socket.
4. The helper does not try to detect insecure ports on a caller-owned server.
   A warning cannot reliably enumerate the ports a server has or will later
   add, so the guarantee is structural but bounded: the helper adds exactly
   one listener and never another. Ports added afterwards to the returned
   server are the caller's own unsupported bypass, exactly as with the
   low-level registration API.

## Consequences

- Breaking change: no. Every change is additive; existing signatures and the
  private Unix-socket use in `run_source` are untouched.
- Plaintext TCP remains reachable only through the low-level API, where it is
  the caller's documented responsibility; the supported paths never enable
  it.
- The testing harness's plaintext loopback endpoint stays until AAP-103447
  migrates it to private IPC, and TLS key access and lifecycle policy is
  AAP-103448; both are explicitly out of scope here.
- The Execution Plane worker's plaintext port-forwarded channel is
  unaffected: the low-level API and its callers are preserved.
- ADR numbering note: open PRs also add files numbered 0015 and 0016; this
  file takes the next number free on devel, matching the existing
  duplicate-tolerant numbering (two 0013s, three 0014s).
