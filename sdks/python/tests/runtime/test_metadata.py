from syntara_plugin.runtime import supports_runtime_probe


def test_runtime_probe_support_matches_registered_probes() -> None:
    assert supports_runtime_probe("python")
    assert supports_runtime_probe("Bash")
    assert not supports_runtime_probe("ruby")
