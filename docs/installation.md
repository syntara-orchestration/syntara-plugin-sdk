# Installation

The released Python distribution installs the `syntara-plugin` command and its
contract dependencies together.

## Install a released SDK

Use one of these commands after the package is published:

```sh
uv tool install syntara-plugin-sdk
# or
python -m pip install syntara-plugin-sdk
```

Verify the installation:

```sh
syntara-plugin --help
```

Use `uv tool upgrade syntara-plugin-sdk` or your normal Python dependency
management process to update the SDK. The installed package is for authors;
you do not need a Syntara server, Podman, or registry to validate and package a
credentialless plugin.

To opt in to an alpha or beta release, install prereleases explicitly:

```sh
uv tool install --prerelease allow syntara-plugin-sdk
# or
python -m pip install --pre syntara-plugin-sdk
```

## Work from the repository

For an unreleased SDK checkout, clone the repository and install all local
packages in editable mode. The SDK command depends on the local contract and
runtime packages, so install the three together:

```sh
git clone https://github.com/syntara-orchestration/syntara-plugin-sdk.git
cd syntara-plugin-sdk
python -m venv .venv
source .venv/bin/activate
python -m pip install \
  --editable ./sdks/python/packages/contracts \
  --editable ./sdks/python/packages/runtime \
  --editable ./sdks/python/packages/sdk
syntara-plugin --help
```

For SDK contributors, `uv` additionally installs test and lint tooling from
the workspace root:

```sh
uv venv
source .venv/bin/activate
uv sync --all-groups --active --inexact
```

Run the focused test suite before changing SDK behavior:

```sh
python -m pytest sdks/python/tests -q
```

Continue with [getting started](getting-started.md) to create a plugin, or use
the [developer guide](developer-guide.md) for contract and contribution work.
