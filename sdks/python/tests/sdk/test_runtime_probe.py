from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from syntara_plugin.sdk import runtime_probe
from syntara_plugin.sdk.runner import RunExecutionError
from syntara_plugin.sdk.runtime_probe import (
    RuntimeProbeError,
    _read_runtime_annotations,
    observe_runtime_image,
)


def _observe(
    monkeypatch,
    *,
    language="python",
    version="3.12.8",
    report=True,
    component_name="python",
    component_executable="/usr/bin/python3.12",
    reported_executable=None,
    component_probe_status="matched",
    component_version="3.12.8",
    expected_metadata=None,
):
    @contextmanager
    def image_available(*args, **kwargs):
        yield "sha256:" + "a" * 64

    @contextmanager
    def managed_runtime(*args, **kwargs):
        yield SimpleNamespace(address="127.0.0.1:50051", tls=None, liveness=None)

    components = ()
    if report:
        components = (
            SimpleNamespace(
                name=component_name,
                executable=reported_executable or component_executable,
                version=component_version,
                probe_status=component_probe_status,
            ),
        )
    monkeypatch.setattr(runtime_probe, "_image_available", image_available)
    monkeypatch.setattr(
        runtime_probe,
        "_read_runtime_annotations",
        lambda _image: {"language": language, "version": version},
    )
    monkeypatch.setattr(runtime_probe, "managed_runtime", managed_runtime)
    monkeypatch.setattr(
        runtime_probe,
        "_read_health_report",
        lambda *args, **kwargs: SimpleNamespace(runtime_components=components),
    )
    monkeypatch.setattr(runtime_probe, "_probe_execution_document", lambda *_args: {})
    return observe_runtime_image(
        "quay.io/acme/workload@sha256:" + "a" * 64,
        [{"name": component_name, "executable": component_executable}],
        descriptor={},
        workload_id="python",
        expected_metadata=expected_metadata,
    )


def test_runtime_probe_matches_image_annotations_against_supported_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _observe(monkeypatch)

    assert observed == {
        "language": "python",
        "version": "3.12.8",
        "probeStatus": "matched",
        "components": [
            {
                "name": "python",
                "executable": "/usr/bin/python3.12",
                "version": "3.12.8",
                "probeStatus": "matched",
            }
        ],
    }


def test_podman_image_inspect_reads_annotations_from_exact_image_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def inspect(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout='{"io.syntara.runtime.language":"python","io.syntara.runtime.version":"3.12.8"}',
        )

    monkeypatch.setattr(runtime_probe.subprocess, "run", inspect)

    assert _read_runtime_annotations("sha256:" + "a" * 64) == {
        "language": "python",
        "version": "3.12.8",
    }
    assert calls[0][-1] == "sha256:" + "a" * 64


def test_podman_image_inspect_rejects_missing_runtime_annotations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_probe.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="{}"),
    )

    with pytest.raises(RuntimeProbeError, match="missing Syntara runtime annotations"):
        _read_runtime_annotations("sha256:" + "a" * 64)


def test_runtime_probe_rejects_annotation_probe_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def image_available(*args, **kwargs):
        yield "sha256:" + "a" * 64

    @contextmanager
    def managed_runtime(*args, **kwargs):
        yield SimpleNamespace(address="127.0.0.1:50051", tls=None, liveness=None)

    monkeypatch.setattr(runtime_probe, "_image_available", image_available)
    monkeypatch.setattr(
        runtime_probe,
        "_read_runtime_annotations",
        lambda _image: {"language": "python", "version": "3.12.9"},
    )
    monkeypatch.setattr(runtime_probe, "managed_runtime", managed_runtime)
    monkeypatch.setattr(
        runtime_probe,
        "_read_health_report",
        lambda *args, **kwargs: SimpleNamespace(
            runtime_components=(
                SimpleNamespace(
                    name="python",
                    executable="/usr/bin/python3.12",
                    version="3.12.8",
                    probe_status="matched",
                ),
            )
        ),
    )
    monkeypatch.setattr(runtime_probe, "_probe_execution_document", lambda *_args: {})

    with pytest.raises(RuntimeProbeError, match="annotation differs"):
        observe_runtime_image(
            "quay.io/acme/workload@sha256:" + "a" * 64,
            [{"name": "python", "executable": "/usr/bin/python3.12"}],
            descriptor={},
            workload_id="python",
        )


def test_runtime_probe_rejects_supported_language_without_matching_health_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        RuntimeProbeError, match="supported runtime component 'python' was not reported"
    ):
        _observe(monkeypatch, report=False)


def test_runtime_probe_rejects_supported_language_with_unsupported_health_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        RuntimeProbeError, match="supported runtime component 'python' did not report a version"
    ):
        _observe(
            monkeypatch,
            component_probe_status="unsupported",
            component_version="",
        )


def test_runtime_probe_rejects_matched_component_without_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        RuntimeProbeError, match="supported runtime component 'python' did not report a version"
    ):
        _observe(monkeypatch, component_probe_status="matched", component_version="")


def test_runtime_probe_rejects_health_component_with_wrong_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeProbeError, match="reports a different executable"):
        _observe(monkeypatch, reported_executable="/opt/python")


def test_runtime_probe_fails_when_health_transport_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def image_available(*args, **kwargs):
        yield "sha256:" + "a" * 64

    @contextmanager
    def managed_runtime(*args, **kwargs):
        yield SimpleNamespace(address="127.0.0.1:50051", tls=None, liveness=None)

    def fail_health(*args, **kwargs):
        raise RunExecutionError("workload did not provide a Health response")

    monkeypatch.setattr(runtime_probe, "_image_available", image_available)
    monkeypatch.setattr(
        runtime_probe,
        "_read_runtime_annotations",
        lambda _image: {"language": "python", "version": "3.12.8"},
    )
    monkeypatch.setattr(runtime_probe, "managed_runtime", managed_runtime)
    monkeypatch.setattr(runtime_probe, "_read_health_report", fail_health)
    monkeypatch.setattr(runtime_probe, "_probe_execution_document", lambda *_args: {})

    with pytest.raises(RuntimeProbeError, match="workload did not provide a Health response"):
        observe_runtime_image(
            "quay.io/acme/workload@sha256:" + "a" * 64,
            [{"name": "python", "executable": "/usr/bin/python3.12"}],
            descriptor={},
            workload_id="python",
        )


def test_runtime_probe_rejects_language_without_a_declared_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeProbeError, match="no matching component declaration"):
        _observe(monkeypatch, language="ruby", component_name="python")


def test_runtime_probe_retains_annotations_as_unsupported_without_a_supported_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _observe(
        monkeypatch,
        language="ruby",
        version="3.3.0",
        component_name="ruby",
        component_executable="/usr/bin/ruby",
    )

    assert observed["language"] == "ruby"
    assert observed["version"] == "3.3.0"
    assert observed["probeStatus"] == "unsupported"
    assert observed["components"] == [
        {"name": "ruby", "executable": "/usr/bin/ruby", "probeStatus": "unsupported"}
    ]


def test_runtime_probe_rejects_settings_that_disagree_with_image_annotations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeProbeError, match="differ from the build settings"):
        _observe(
            monkeypatch,
            expected_metadata={"language": "python", "version": "3.12.9"},
        )
