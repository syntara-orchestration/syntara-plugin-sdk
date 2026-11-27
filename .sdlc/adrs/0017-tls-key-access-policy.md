# ADR 0017: TLS key access policy

- Status: Accepted
- Date: 2026-11-25

## Context

Managed local runs generated one throwaway CA with server and client
certificates and chmodded every file, including both private keys, to 0644
so any non-root image user could read the mounted material. A per-file bind
mount bypasses the 0700 temporary directory that holds the material, so the
world-readable server key was readable by every identity that could reach
the mount, and the host-only client and CA keys carried a needless 0644
mode. The runtime server accepted any key it could read and failed startup
with a bare read error when it could not, and the transport's TLS floor was
undocumented.

## Decision

Generated material follows a restricted policy: the CA and client keys are
owner-only (0600) and never mounted; the server key is 0640 in the invoking
user's primary group; certificates stay 0644. The managed run grants the
container exactly that group's container-side id as a supplemental group:
gid 0 under rootless engines, which present the primary group as gid 0, and
the primary gid under rootful engines, which map group ids unchanged. The
mode is selected by asking the engine (`podman info`, `docker info`)
whether it runs rootless, because an unmapped group id makes the engine
refuse to start the container at all. The flag applies to whatever user the
selected image declares, so no runtime-identity resolution is needed. When
the engine cannot answer, the run fails with guidance instead of guessing a
mapping. Host-side, the material directory is created 0700, so the 0640
server key stays private to the invoking user even where the primary group
is shared.

The runtime server checks its TLS key at startup: it resolves symlinks,
verifies the process can actually read the key, accepts every documented
restricted arrangement including projected secret mounts whose access flows
through other-read, and refuses a world-readable key only when a restricted
arrangement already works for the process. Diagnostics name paths, modes,
and owners, never key contents.

The supported transport floor is TLS 1.2 as implemented by the grpcio
bundled BoringSSL; its Python API exposes no version or cipher overrides,
and none are invented. mTLS remains mandatory for TCP listeners.

## Consequences

This is a deployment-policy change: key mounts that previously relied on a
world-readable mode can now be refused at startup, and an engine that
cannot report its rootless mode fails the managed run fast instead of
starting a container that cannot read its key. The restricted delivery is
verified on Linux rootless Podman; the rootful group id is chosen by the
same code path and covered by unit tests. Hosts whose engines run behind a
VM boundary (Docker Desktop, podman machine on macOS) present bind mounts
with their own ownership, which no host-side group grant can match, so the
managed run refuses on non-Linux hosts until that presentation is
verified and a correct grant is designed. The Execution
Plane and any deployment that mounts its own TLS material should deliver
the key through its owner, its group, or a projected secret, and must
monitor long-lived certificate validity externally: the runtime performs no
revocation, SAN authorization, or renewal.
