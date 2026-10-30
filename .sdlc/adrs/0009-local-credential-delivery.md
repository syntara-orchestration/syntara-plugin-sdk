# ADR 0009: Local credential delivery for `syntara-plugin run`

- Status: Accepted
- Date: 2026-10-30

## Context

`syntara-plugin run` executes one custom-runner Step locally but always sent
an empty credential map, so a Step that declares an `integrationType` could
not be validated locally at all. `--credential NAME=SOURCE` closes that gap.

The constraint that shapes the design is the container ABI's approved secret
mount. `credential_file_paths` carries paths, never values, and both the
client encoder and the server decoder require every path to match
`^/var/run/syntara/secrets/[A-Za-z0-9._-]+$`. A local run cannot write under
`/var/run` without privilege, so the two execution modes cannot reach that
path the same way:

- image mode starts the container itself and can mount a file anywhere in it;
- source mode serves an in-process gRPC runtime over a unix socket, and the
  "container" is this process, whose filesystem is the host's.

A second constraint is sequencing. The step-side credential accessor
(AAP-95450) is not implemented: `ExecutionContext` has no credential field
and the adapter drops `credential_file_paths`. This story therefore delivers
the credential without anything in the SDK reading it back.

## Decision

1. **One mechanism, two destinations.** Each invocation gets one private
   directory from `mkdtemp` (owner-only) holding one file per credential,
   written from memory and removed when the invocation ends. Image mode
   bind-mounts each file read-only at `/var/run/syntara/secrets/<name>` and
   sends that in-container path; source mode sends the real host path.

2. **A default-off local exception, not a wider rule.** `ContainerClient`,
   `ContainerRuntimeService`, and `add_container_runtime_service` each take
   an optional local credential directory. When set, a file *directly inside*
   that directory is accepted alongside the approved mount; both sides are
   resolved with `realpath` first, so neither a `..` segment nor a symlink
   can name a file outside it. Only `run_source` sets it, for the one
   transport where client and runtime are the same process. A deployed
   workload never sets it and keeps the production rule unchanged. The
   alternative — widening the shared `_CREDENTIAL_PATH` regex — was rejected
   because it would relax the rule for every client and server, including
   the Execution Plane's.

3. **Bind mounts, not the engine's secret store.** `podman secret create`
   was the obvious candidate but was rejected on two findings: `docker run`
   has no `--secret` at all (verified against Podman 5.8.4 and Docker
   29.9.2), so it would support only one engine; and Podman's default `file`
   driver writes the secret into container storage, where it persists until
   `podman secret rm`, which is a weaker guarantee than a file removed when
   the block ends. A read-only bind mount works identically on both engines,
   is the same mechanism source mode already needs, and reuses the mount
   handling the managed container's TLS material uses.

4. **Delivery files are `0644` in image mode, `0600` in source mode.** The
   container's runtime user is not the host user, so an owner-only file is
   unreadable inside the container; the owner-only *directory* is what
   protects it on the host. This mirrors the existing treatment of the
   managed container's TLS private key.

5. **No inline values.** `--credential NAME=VALUE` is refused, as a value on
   the command line is visible in shell history and `/proc/<pid>/cmdline`.
   This matches `--password-stdin` and the settings-file secret rules in
   ADR 0006. The refusal message never echoes the refused text.

6. **`--credential` is rejected with `--address` and `--tls-*`.** The author
   owns that runtime; the command cannot mount a file into it, and inventing
   a second delivery channel for it would not match production.

## Consequences

- A plugin that requires a credential can be validated with the same local
  run workflow as any other plugin, through the same delivery shape
  production will use.
- The ABI's approved-mount rule is unchanged for every caller that does not
  explicitly opt in, and the opt-in is a directory, not a pattern.
- Source mode's delivery path is a host path rather than the approved mount.
  Step code that hard-codes `/var/run/syntara/secrets/...` instead of using
  the path it is given will behave differently in the two modes; the typed
  accessor in AAP-95450 is what removes that sharp edge.
- Until that accessor lands, the file arrives but nothing in the SDK hands
  its path to step code, so the local credential path is verifiable but not
  yet usable from a Step.
- Credentials are delivered only for a container this command starts, so
  `--address` users keep supplying their runtime's credentials themselves.
