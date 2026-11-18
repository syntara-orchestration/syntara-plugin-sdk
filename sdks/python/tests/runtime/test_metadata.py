import json
import sys
from types import SimpleNamespace

import pytest
from syntara_plugin.runtime import metadata, supports_runtime_probe


def test_runtime_probe_support_matches_registered_probes() -> None:
    assert supports_runtime_probe("python")
    assert supports_runtime_probe("Bash")
    assert not supports_runtime_probe("ruby")


def test_runtime_probe_reports_match_for_supported_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata, "_probe_for_name", lambda _name: lambda _executable: "3.12.8")

    observed = metadata.probe_runtime_components(
        [{"name": "custom", "executable": "/opt/runtime"}]
    )

    assert observed == (
        {
            "name": "custom",
            "executable": "/opt/runtime",
            "version": "3.12.8",
            "probe_status": "matched",
        },
    )


def test_runtime_probe_marks_unknown_component_unsupported() -> None:
    assert metadata.probe_runtime_components(
        [{"name": "ruby", "executable": "/usr/bin/ruby"}]
    ) == (
        {"name": "ruby", "executable": "/usr/bin/ruby", "probe_status": "unsupported"},
    )


def test_runtime_probe_rejects_invalid_supported_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata, "_probe_for_name", lambda _name: lambda _executable: "3.12\nmalicious")

    with pytest.raises(metadata.RuntimeComponentProbeError, match="invalid version"):
        metadata.probe_runtime_components(
            [{"name": "custom", "executable": "/opt/runtime"}]
        )


def test_runtime_probe_fails_when_declared_executable_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        metadata.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=b""),
    )

    with pytest.raises(metadata.RuntimeComponentProbeError, match="probe failed"):
        metadata._run_probe("/opt/runtime", ["--version"])


def test_python_probe_reads_version_and_requires_the_declared_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        metadata,
        "_run_probe",
        lambda *_args: json.dumps({"version": "3.12.8", "executable": sys.executable}),
    )

    assert metadata._probe_python(sys.executable) == "3.12.8"


def test_python_probe_rejects_a_different_running_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        metadata,
        "_run_probe",
        lambda *_args: json.dumps({"version": "3.12.8", "executable": "/opt/python"}),
    )

    with pytest.raises(metadata.RuntimeComponentProbeError, match="differs"):
        metadata._probe_python(sys.executable)


def test_bash_probe_requires_gnu_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        metadata, "_run_probe", lambda *_args: "GNU bash, version 5.2.26(1)-release"
    )

    assert metadata._probe_bash("/usr/bin/bash") == "5.2.26"


def test_bash_probe_rejects_an_unrecognized_version_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "_run_probe", lambda *_args: "bash version unknown")

    with pytest.raises(metadata.RuntimeComponentProbeError, match="not GNU Bash"):
        metadata._probe_bash("/usr/bin/bash")
