"""Check that EP imports its checkout and the SDK contracts under test."""

import importlib
import sys
from pathlib import Path

import execution_plane
import execution_plane_client
import syntara_plugin.runtime
from syntara_plugin.contracts import load_bundle


def main() -> None:
    ep = Path(sys.argv[1]).resolve()
    sdk = Path(sys.argv[2]).resolve()
    assert Path(execution_plane.__file__).resolve().is_relative_to(ep / "src")
    assert Path(execution_plane_client.__file__).resolve().is_relative_to(ep / "src")
    assert (
        Path(syntara_plugin.runtime.__file__)
        .resolve()
        .is_relative_to(sdk / "sdks/python/packages/contracts/src")
    )
    assert load_bundle().digest
    # Check the actual EP adapter as well as namespace-package provenance.
    importlib.import_module("execution_plane.worker_manager.vanilla_k8s.sdk_adaptor")


if __name__ == "__main__":
    main()
