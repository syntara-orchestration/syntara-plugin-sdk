# Releasing the Python packages

The repository publishes three Python packages as one versioned release train:

1. `syntara-plugin-contracts`
2. `syntara-plugin-runtime`
3. `syntara-plugin-sdk`

The runtime pins the matching contracts release and the SDK pins the matching
runtime and contracts releases. This makes the protocol and contract bundle
used by a released CLI explicit and reproducible.

## One-time PyPI and GitHub setup

Do this before the first release. Do not create or store a PyPI API token in
GitHub.

1. In the PyPI account that will own the projects, create a pending GitHub
   Trusted Publisher for each package name above. Use these values:

   | Setting | Value |
   | --- | --- |
   | Owner | `syntara-orchestration` |
   | Repository | `syntara-plugin-sdk` |
   | Workflow | `publish.yml` |
   | Environment | `pypi` |

2. In the GitHub repository, create the `pypi` environment. Require approval
   from the designated release maintainers and limit deployment branches or
   tags to protected release tags.
3. Protect `devel` and the `v*` release-tag pattern so that only the release
   maintainers can create a release from reviewed code.

The initial Trusted Publisher configuration is *pending* only until its first
successful package upload. It does not reserve a package name, so register the
publishers shortly before the first release and verify the names carefully.

## Release an alpha

All four workspace project versions must match the release tag without its
`v` prefix. For the first prerelease, the version is `0.1.0a1` and the GitHub
release tag is `v0.1.0a1`.

1. Merge the reviewed release preparation pull request into `devel`.
2. Create a protected GitHub release from the merged commit using the matching
   `v<version>` tag. Mark an alpha or beta as a prerelease in GitHub.
3. Approve the three `pypi` environment deployments when GitHub asks. The
   workflow builds and verifies all distributions once, then publishes the
   contract package, runtime package, and SDK package in that order.
4. Verify the package installation and its PyPI attestation:

   ```sh
   python -m pip install --pre syntara-plugin-sdk
   syntara-plugin --help
   python -m pip install pypi-attestations
   pypi-attestations verify pypi \
     --repository https://github.com/syntara-orchestration/syntara-plugin-sdk \
     "https://files.pythonhosted.org/.../syntara_plugin_sdk-<version>-py3-none-any.whl"
   ```

Use the wheel URL shown on the PyPI release page for the verification command.
PyPI records the Trusted Publisher identity and uploads a Sigstore-backed
attestation for each source and wheel distribution.

## Safety properties

- The workflow only responds to a published GitHub release. It does not run on
  pull requests, issue comments, or `pull_request_target`.
- The build job has read-only repository access and performs tests, linting,
  metadata checks, and a clean-environment install before passing immutable
  build artifacts to the publishing jobs.
- Each publisher job has only artifact-read and OIDC-token permission, runs no
  repository code, and invokes the official PyPA action once.
- The workflow is pinned to immutable action commits. Update those pins through
  normal review when an upstream security release is available. The Python
  build backend and metadata checker are pinned as well.
- The `zizmor` workflow audits changes to GitHub Actions definitions for known
  dangerous patterns before they can reach the release workflow.
