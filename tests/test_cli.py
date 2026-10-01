import json
from pathlib import Path

import pytest
import yaml
from syntara_tools.cli import build_step, init_step, main
from syntara_tools.oci_client import OCI_ARTIFACT_TYPE, OCI_MANIFEST_ANNOTATION


def test_init_tier_two_creates_script_package(tmp_path: Path) -> None:
    target = tmp_path / "normalize_payload"
    init_step(target, "normalize_payload", 2, None, base_dir=tmp_path)
    assert (target / "main.py").exists()
    assert (target / "manifest.yaml").exists()
    manifest = yaml.safe_load((target / "manifest.yaml").read_text())
    assert "version" not in manifest["metadata"]
    assert "namespace" not in manifest["metadata"]
    assert manifest["metadata"]["tags"] == []


def test_init_tier_three_creates_dedicated_package(tmp_path: Path) -> None:
    target = tmp_path / "custom_step"
    init_step(target, "custom_step", 3, "quay.io/example/custom-step:1.0.0", base_dir=tmp_path)
    assert (target / "Containerfile").exists()
    assert (target / "manifest.yaml").exists()


ARTIFACT_REF = "quay.io/example/plugins/custom-step:1.0.0"
RUNTIME_IMAGE = "quay.io/example/custom-step:1.0.0"


def test_build_writes_oci_manifest_annotation(tmp_path: Path) -> None:
    source = tmp_path / "custom_step"
    init_step(source, "custom_step", 3, RUNTIME_IMAGE, base_dir=tmp_path)
    output = tmp_path / "step-manifest.json"

    build_step(source / "manifest.yaml", output, ARTIFACT_REF)

    artifact = json.loads(output.read_text())
    assert artifact["artifactType"] == OCI_ARTIFACT_TYPE
    assert OCI_MANIFEST_ANNOTATION in artifact["annotations"]
    assert "metadata:" in artifact["annotations"][OCI_MANIFEST_ANNOTATION]


def test_artifact_ref_does_not_overwrite_runtime_image(tmp_path: Path) -> None:
    """The packaged plugin artifact and the runtime image are distinct refs."""

    source = tmp_path / "custom_step"
    init_step(source, "custom_step", 3, RUNTIME_IMAGE, base_dir=tmp_path)
    output = tmp_path / "step-manifest.json"

    build_step(source / "manifest.yaml", output, ARTIFACT_REF)
    artifact = json.loads(output.read_text())

    # Published-to location is the artifact ref...
    assert artifact["annotations"]["org.opencontainers.image.ref.name"] == ARTIFACT_REF
    # ...while the embedded manifest still names the runtime the step runs in.
    embedded = yaml.safe_load(artifact["annotations"][OCI_MANIFEST_ANNOTATION])
    assert embedded["spec"]["execution"]["image"] == RUNTIME_IMAGE


def test_build_requires_an_artifact_reference(tmp_path: Path) -> None:
    source = tmp_path / "custom_step"
    init_step(source, "custom_step", 3, RUNTIME_IMAGE, base_dir=tmp_path)

    with pytest.raises(ValueError, match="artifact reference"):
        build_step(source / "manifest.yaml", tmp_path / "out.json", "")


@pytest.mark.parametrize(
    "hostile",
    [
        Path("../escaped"),
        Path("../../etc/cron.d"),
        Path("nested/../../escaped"),
    ],
)
def test_init_rejects_paths_escaping_the_base(tmp_path: Path, hostile: Path) -> None:
    """--path may be model-generated in an agentic workflow; it must stay in-tree."""

    with pytest.raises(ValueError, match="escapes the base directory"):
        init_step(hostile, "custom_step", 3, RUNTIME_IMAGE, base_dir=tmp_path)

    assert not (tmp_path.parent / "escaped").exists()


def test_init_rejects_absolute_path_outside_base(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_target"

    with pytest.raises(ValueError, match="escapes the base directory"):
        init_step(outside, "custom_step", 3, RUNTIME_IMAGE, base_dir=tmp_path)

    assert not outside.exists()


def test_init_still_allows_a_nested_in_tree_path(tmp_path: Path) -> None:
    init_step(Path("plugins/custom_step"), "custom_step", 3, RUNTIME_IMAGE, base_dir=tmp_path)

    assert (tmp_path / "plugins" / "custom_step" / "manifest.yaml").exists()


def test_validate_detects_step_and_plugin_resources(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    step_path = tmp_path / "step"
    init_step(step_path, "step", 3, RUNTIME_IMAGE, base_dir=tmp_path)
    assert main(["validate", str(step_path / "manifest.yaml")]) == 0
    assert "validated step manifest" in capsys.readouterr().out

    plugin = {
        "apiVersion": "syntara.io/v1alpha1",
        "kind": "Plugin",
        "metadata": {
            "name": "sample",
            "namespace": "syntara",
            "displayName": "Sample",
            "version": "0.1.0",
            "description": "Sample plugin.",
            "authors": [{"name": "Example"}],
        },
        "spec": {"targets": ["step/manifest.yaml"]},
    }
    (tmp_path / "plugin.yaml").write_text(yaml.safe_dump(plugin), encoding="utf-8")
    assert main(["validate", str(tmp_path / "plugin.yaml")]) == 0
    assert "validated plugin with 1 step" in capsys.readouterr().out
