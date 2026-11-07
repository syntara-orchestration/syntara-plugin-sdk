# ADR 0010: Build release objects locally and publish them as a reviewed set

- Status: Accepted
- Date: 2026-11-03
- Supersedes: The combined `build --with-workload` flow in ADR 0006

## Context

A plugin release can contain three independently versioned OCI objects:

- P: the plugin metadata artifact, with its step contracts and documentation;
- I: one or more workload images; and
- B: a RuntimeBinding artifact that links a plugin digest to image digests.

These objects have different owners and security-update lifecycles. A workload
image can need a CVE fix while the plugin contract stays unchanged. Building
and publishing them in one command hides which exact bytes were reviewed and
makes it harder to retry a partially completed registry operation.

## Decision

1. `syntara-plugin build` writes local OCI archives and resolved metadata. It
   does not push to a registry. Registry publication is an explicit
   `syntara-plugin publish` operation.
2. A multi-object build writes a `PluginReleaseInventory` in `dist/release.json`.
   The inventory records the expected digests, repositories, image build
   targets, and local archive paths relative to `dist`. It contains no
   credentials or signing keys and can move with the release directory.
3. Release archives must stay beneath `dist`; publication rejects absolute,
   traversing, and symlinked archive paths. Build refuses to overwrite existing
   archives and removes outputs it created if a multi-image build fails.
4. `publish --artifact-kind release` verifies every local archive and digest,
   then publishes workload images, a new plugin artifact when requested, the
   RuntimeBinding, and its successor index. A new plugin release may update
   the plugin catalog after these release objects succeed. An image-only
   update reuses the exact existing plugin artifact and does not change its
   catalog entry.
5. Cosign signing is applied during publication to each published immutable
   reference when a signing key is supplied. Signing and catalog policy remain
   explicit deployment choices; local build does not claim a release is
   trusted.

The existing `build --with-workload` and `build --publish` options are not part
of the current interface. The older metadata-only `build` command remains
available for plugins that do not need RuntimeBindings.

## Consequences

- Authors and release automation can inspect and retain the exact P, I, and B
  archives before uploading them.
- An image-only security update can publish a new image and binding while
  leaving the plugin metadata digest unchanged.
- Registry publication is not an atomic transaction. The command writes a
  publication receipt after each completed phase so an operator can identify
  what succeeded and retry using immutable digests.
- This SDK release workflow does not by itself prove that the Execution Plane
  can schedule, verify, and run every supported workload. End-to-end runtime
  conformance and Syntara's production installation policy remain release
  gates for claiming the full product workflow is production ready.
