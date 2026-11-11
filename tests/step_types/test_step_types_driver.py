"""Tests for the driver's injection and discovery helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
from discover import DiscoveryError, sdk_plugins
from driver import (
    StageFailed,
    Wheels,
    candidate_problems,
    find_wheels,
    import_problems,
    parse_args,
    patch_sources,
    workload_tag,
)

SDK = Path("/w/syntara_plugin_sdk-1.0-py3-none-any.whl")
CONTRACTS = Path("/w/syntara_plugin_contracts-1.0-py3-none-any.whl")


def make_wheel(directory: Path, name: str, version: str, requires: str = "") -> Path:
    path = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{requires}"
        archive.writestr(f"{name}-{version}.dist-info/METADATA", metadata)
    return path


def test_patch_sources_appends_a_table() -> None:
    patched = patch_sources('[project]\nname = "x"\n', {"syntara-plugin-sdk": SDK})
    assert tomllib.loads(patched)["tool"]["uv"]["sources"]["syntara-plugin-sdk"] == {
        "path": str(SDK)
    }


def test_patch_sources_merges_with_an_existing_table() -> None:
    text = (
        "[tool.uv.sources]\n"
        'other = { workspace = true }\n'
        'syntara-plugin-sdk = { index = "old" }\n'
        "\n[tool.pytest.ini_options]\naddopts = \"-q\"\n"
    )
    patched = patch_sources(text, {"syntara-plugin-sdk": SDK, "syntara-plugin-contracts": CONTRACTS})
    document = tomllib.loads(patched)
    sources = document["tool"]["uv"]["sources"]
    assert sources["other"] == {"workspace": True}
    assert sources["syntara-plugin-sdk"] == {"path": str(SDK)}
    assert sources["syntara-plugin-contracts"] == {"path": str(CONTRACTS)}
    assert document["tool"]["pytest"]["ini_options"]["addopts"] == "-q"


def test_patch_sources_rejects_conflicting_inline_sources() -> None:
    text = '[tool.uv]\nsources = { syntara-plugin-sdk = { index = "x" } }\n'
    with pytest.raises((ValueError, tomllib.TOMLDecodeError)):
        patch_sources(text, {"syntara-plugin-sdk": SDK})


def test_find_wheels_accepts_a_consistent_pair(tmp_path: Path) -> None:
    make_wheel(tmp_path, "syntara_plugin_sdk", "1.0", "Requires-Dist: syntara-plugin-contracts==1.0\n")
    make_wheel(tmp_path, "syntara_plugin_contracts", "1.0")
    make_wheel(tmp_path, "syntara_plugin_extra", "1.0")
    wheels = find_wheels(tmp_path)
    assert (wheels.sdk_version, wheels.contracts_version) == ("1.0", "1.0")
    assert set(wheels.paths) == {
        "syntara-plugin-sdk",
        "syntara-plugin-contracts",
        "syntara-plugin-extra",
    }


def test_find_wheels_rejects_an_inconsistent_pair(tmp_path: Path) -> None:
    make_wheel(tmp_path, "syntara_plugin_sdk", "1.0", "Requires-Dist: syntara-plugin-contracts==2.0\n")
    make_wheel(tmp_path, "syntara_plugin_contracts", "1.0")
    with pytest.raises(StageFailed, match="inconsistent"):
        find_wheels(tmp_path)


def test_find_wheels_requires_both_wheels(tmp_path: Path) -> None:
    make_wheel(tmp_path, "syntara_plugin_sdk", "1.0")
    with pytest.raises(StageFailed, match="syntara-plugin-contracts wheel not found"):
        find_wheels(tmp_path)


def test_candidate_problems_detects_index_installs(tmp_path: Path) -> None:
    sdk = make_wheel(tmp_path, "syntara_plugin_sdk", "1.0")
    contracts = make_wheel(tmp_path, "syntara_plugin_contracts", "1.0")
    wheels = Wheels({"syntara-plugin-sdk": sdk, "syntara-plugin-contracts": contracts})
    good = {"url": f"file://{sdk}", "archive_info": {}}
    report = {
        "syntara-plugin-sdk": {"version": "1.0", "direct_url": json.dumps(good)},
        "syntara-plugin-contracts": {"version": "1.0", "direct_url": None},
    }
    problems = candidate_problems(report, wheels)
    assert problems == ["syntara-plugin-contracts 1.0 is installed from an index"]


def test_import_problems_accepts_candidate_imports(tmp_path: Path) -> None:
    sdk = make_wheel(tmp_path, "syntara_plugin_sdk", "1.0")
    contracts = make_wheel(tmp_path, "syntara_plugin_contracts", "1.0")
    wheels = Wheels({"syntara-plugin-sdk": sdk, "syntara-plugin-contracts": contracts})
    entry = {
        "argv": ["syntara-plugin", "validate"],
        "syntara-plugin-sdk": {"version": "1.0", "direct_url": json.dumps({"url": f"file://{sdk}"})},
        "syntara-plugin-contracts": {
            "version": "1.0",
            "direct_url": json.dumps({"url": f"file://{contracts}"}),
        },
    }
    assert import_problems(json.dumps(entry) + "\n", wheels) == []


def test_import_problems_flags_a_published_sdk(tmp_path: Path) -> None:
    sdk = make_wheel(tmp_path, "syntara_plugin_sdk", "1.0")
    contracts = make_wheel(tmp_path, "syntara_plugin_contracts", "1.0")
    wheels = Wheels({"syntara-plugin-sdk": sdk, "syntara-plugin-contracts": contracts})
    overlay = {
        "argv": ["syntara-plugin", "validate"],
        "syntara-plugin-sdk": {"version": "0.9", "direct_url": None},
        "syntara-plugin-contracts": {
            "version": "0.9",
            "direct_url": json.dumps({"url": "file:///cache/archive/other.whl"}),
        },
    }
    bare = {"argv": ["python"], "syntara-plugin-sdk": None, "syntara-plugin-contracts": None}
    problems = import_problems(f"{json.dumps(overlay)}\n{json.dumps(bare)}\nnot json\n", wheels)
    assert problems == [
        "python imported syntara_plugin without an installed SDK distribution",
        "syntara-plugin validate imported syntara-plugin-contracts 0.9 from "
        "file:///cache/archive/other.whl",
        "syntara-plugin validate imported syntara-plugin-sdk 0.9 from a package index",
    ]


def test_patch_sources_replaces_differently_spelled_keys() -> None:
    text = '[tool.uv.sources]\nsyntara_plugin_sdk = { index = "x" }\n"syntara-plugin-sdk" = { index = "y" }\n'
    document = tomllib.loads(patch_sources(text, {"syntara-plugin-sdk": SDK}))
    assert document["tool"]["uv"]["sources"] == {"syntara-plugin-sdk": {"path": str(SDK)}}


def test_parse_args_rejects_path_like_plugin_names() -> None:
    with pytest.raises(SystemExit) as raised:
        parse_args(["--plugin", "../escaped", "--step-types-dir", "s", "--wheels-dir", "w",
                    "--report-dir", "r"])
    assert raised.value.code == 2


def test_workload_tag_matches_step_types_pattern() -> None:
    tag = workload_tag({"GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40})
    assert tag == "ci-1-2-" + "a" * 40
    assert len(tag) <= 128


@pytest.mark.skipif(shutil.which("make") is None, reason="make is required")
def test_discovery_reads_sdk_plugins(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("SDK_PLUGINS := beta alpha\n")
    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "syntara-plugin.yaml").write_text("{}\n")
    assert sdk_plugins(tmp_path, shutil.which("make") or "make") == ["alpha", "beta"]


@pytest.mark.skipif(shutil.which("make") is None, reason="make is required")
@pytest.mark.parametrize(
    ("makefile", "message"),
    [
        ("OTHER := x\n", "at least one plugin"),
        ("SDK_PLUGINS := Bad_Name\n", "invalid plugin name"),
        ("SDK_PLUGINS := ghost\n", "without a syntara-plugin.yaml"),
    ],
)
def test_discovery_refuses_to_test_nothing(tmp_path: Path, makefile: str, message: str) -> None:
    (tmp_path / "Makefile").write_text(makefile)
    make = shutil.which("make") or "make"
    with pytest.raises(DiscoveryError, match=message):
        sdk_plugins(tmp_path, make)


def test_discover_writes_github_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("make") is None or shutil.which("git") is None:
        pytest.skip("make and git are required")
    import discover

    repo = tmp_path / "step-types"
    (repo / "only").mkdir(parents=True)
    (repo / "only" / "syntara-plugin.yaml").write_text("{}\n")
    (repo / "Makefile").write_text("SDK_PLUGINS := only\n")
    for command in (
        ["git", "init", "-q"],
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "add", "."],
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "x"],
    ):
        subprocess.run(command, cwd=repo, check=True)
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert discover.main([str(repo)]) == 0
    lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert lines["plugins"] == '["only"]'
    assert len(lines["pin"]) == 40


def test_command_crash_closes_the_stop_commands_region(tmp_path: Path) -> None:
    import io

    from driver import Driver
    from render import Renderer

    stream = io.StringIO()
    out = Renderer(stream, env={}, color=False, github=True, width=80)
    args = parse_args(["--plugin", "http-request", "--step-types-dir", str(tmp_path),
                       "--wheels-dir", str(tmp_path), "--report-dir", str(tmp_path / "r")])
    driver = Driver(args, out)
    try:
        command = [str(tmp_path / "missing-binary")]
        with pytest.raises(FileNotFoundError):
            driver.run(command, cwd=tmp_path, stage="run", title="x")
    finally:
        driver.cleanup()
    lines = stream.getvalue().splitlines()
    token = next(line for line in lines if line.startswith("::stop-commands::")).split("::")[2]
    assert f"::{token}::" in lines
    assert lines[-1] == "::endgroup::"
