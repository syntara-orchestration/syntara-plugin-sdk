"""Exercise the compatibility fixture construction and skip detection without Kind."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load(name: str):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_image_context_derives_resolved_bindings_and_avoids_checkout_build_hooks(
    tmp_path, monkeypatch
):
    helper = _load("prepare_image")
    commands = []
    monkeypatch.setattr(
        helper.subprocess, "run", lambda command, **kwargs: commands.append(command)
    )
    context = tmp_path / "context"
    manifest = helper.prepare(context)
    resolved = json.loads((context / "resolved.json").read_text())
    assert len(manifest["steps"]) == 3
    assert resolved["bindings"] == [
        {"stepRevisionId": revision, "runtimeKind": "custom-runner", **entry}
        for revision, entry in manifest["steps"].items()
    ]
    assert {entry["stepIdentity"] for entry in manifest["steps"].values()} == {
        "example.existing-step.create-ticket",
        "example.existing-step.normalize-records",
        "example.existing-step.record-opened",
    }
    export = commands[0]
    builds = [command for command in commands if command[0:2] == ["uv", "build"]]
    assert export[1:4] == ["export", "--locked", "--package"]
    assert "--no-emit-workspace" in export
    for command in builds:
        assert "--no-build-isolation" in command
        project = Path(command[command.index("--directory") + 1])
        assert project.is_relative_to(context)
        assert "hooks" not in (project / "pyproject.toml").read_text()
    assert len(builds) == 2
    assert (
        context
        / "projects/syntara_plugin/src/syntara_plugin/contracts/schemas/v1alpha1/plugin.schema.json"
    ).is_file()
    assert (
        "SYNTARA_RESOLVED_EXECUTION=/opt/syntara/resolved.json"
        in (context / "Containerfile").read_text()
    )
    containerfile = (context / "Containerfile").read_text()
    assert "pip download --only-binary :all: --require-hashes" in containerfile
    assert "pip install --no-index --find-links /tmp/deps --require-hashes" in containerfile
    assert "pip install --no-index --no-deps /wheels/*.whl" in containerfile
    assert "pip check" in containerfile
    assert "SYNTARA_RUNTIME_GRPC_ADDRESS=127.0.0.1:50052" in containerfile
    assert "COPY certificates/ /opt/syntara/certificates/" in containerfile
    assert "SYNTARA_RUNTIME_GRPC_TLS_CERT=/opt/syntara/certificates/server.crt" in containerfile
    assert "SYNTARA_RUNTIME_GRPC_TLS_KEY=/opt/syntara/certificates/server.key" in containerfile
    assert (
        "SYNTARA_RUNTIME_GRPC_TLS_CLIENT_CA=/opt/syntara/certificates/client-ca.crt"
        in containerfile
    )
    assert 'ENTRYPOINT ["python3.12", "/opt/syntara/runtime_entrypoint.py"]' in containerfile
    assert (context / "runtime_entrypoint.py").is_file()
    assert (context / "tls_bridge.py").is_file()
    assert 'context.set_alpn_protocols(["h2"])' in (context / "tls_bridge.py").read_text()


def test_ci_workflow_tracks_ep_devel_and_records_resolved_revision() -> None:
    workflow = (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text()
    assert "EP_REF: ${{ inputs['ep-ref'] || 'devel' }}" in workflow
    assert 'EP_REVISION=$(git -C execution-plane rev-parse HEAD)' in workflow


def test_credential_workload_context_stages_recipe_and_locked_image(tmp_path, monkeypatch):
    helper = _load("prepare_image")
    commands = []
    monkeypatch.setattr(
        helper.subprocess, "run", lambda command, **kwargs: commands.append(command)
    )
    context = tmp_path / "context"
    manifest = helper.prepare_credential_workload(context)
    resolved = json.loads((context / "resolved.json").read_text())
    assert len(manifest["steps"]) == 3
    assert resolved["bindings"] == [
        {"stepRevisionId": revision, "runtimeKind": "custom-runner", **entry}
        for revision, entry in manifest["steps"].items()
    ]
    assert {entry["stepIdentity"] for entry in manifest["steps"].values()} == {
        "compat.credential-steps.script-bash",
        "compat.credential-steps.script-python",
        "compat.credential-steps.http-request",
    }
    steps = context / "projects/credential_steps/src/credential_steps/manifests/steps"
    connection = json.loads((steps / "connection.yaml").read_text())
    recipe = json.loads((steps / "bearer-token.yaml").read_text())
    assert connection["spec"]["credentialTypes"] == ["compat.credentials.bearer-token"]
    assert recipe["spec"]["id"] == "compat.credentials.bearer-token"
    assert recipe["spec"]["integrationType"] == "compat.credentials.connection"
    containerfile = (context / "Containerfile").read_text()
    assert "SYNTARA_RESOLVED_EXECUTION=/opt/syntara/resolved.json" in containerfile
    assert "pip download --only-binary :all: --require-hashes" in containerfile
    assert "pip install --no-index --no-deps /wheels/*.whl" in containerfile
    assert 'ENTRYPOINT ["python3.12", "/opt/syntara/runtime_entrypoint.py"]' in containerfile
    builds = [command for command in commands if command[0:2] == ["uv", "build"]]
    assert len(builds) == 2


@pytest.mark.parametrize(
    "source",
    [
        "package",
        "package-root",
        "package-parent",
        "contracts-source",
        "contract-data",
        "runtime-metadata",
        "containerfile",
        "credential-package",
        "credential-runtime-metadata",
        "credential-containerfile",
    ],
)
def test_image_context_rejects_symlinked_sources_before_staging(tmp_path, monkeypatch, source):
    helper = _load("prepare_image")
    root = tmp_path / "checkout"
    example = root / "examples/existing-step"
    contracts = root / "sdks/python/packages/contracts"
    credential = root / "tests/execution_plane/credential_workload"
    (example / "src/existing_step").mkdir(parents=True)
    (contracts / "src/syntara_plugin").mkdir(parents=True)
    (credential / "src/credential_steps").mkdir(parents=True)
    (root / "contracts").mkdir()
    for directory in ("schemas", "fixtures", "proto"):
        (root / "contracts" / directory).mkdir()
    (example / "pyproject.toml").write_text("[project]\n")
    (contracts / "pyproject.toml").write_text("[project]\n")
    (credential / "pyproject.toml").write_text("[project]\n")
    (example / "runtime.json").write_text("{}")
    (example / "Containerfile").write_text("FROM scratch\n")
    (credential / "runtime.json").write_text("{}")
    (credential / "Containerfile").write_text("FROM scratch\n")
    target = tmp_path / "runner-secret"
    target.write_text("must not be staged")
    if source == "package":
        (example / "src/existing_step/leak.py").symlink_to(target)
    elif source == "package-root":
        (example / "src/existing_step").rmdir()
        (example / "src/existing_step").symlink_to(target)
    elif source == "package-parent":
        external = tmp_path / "external"
        (external / "existing_step").mkdir(parents=True)
        (external / "existing_step/leak.py").write_text("must not be staged")
        (example / "src/existing_step").rmdir()
        (example / "src").rmdir()
        (example / "src").symlink_to(external, target_is_directory=True)
    elif source == "contracts-source":
        (contracts / "src/syntara_plugin").rmdir()
        (contracts / "src/syntara_plugin").symlink_to(target)
    elif source == "contract-data":
        (root / "contracts/fixtures/leak.json").symlink_to(target)
    elif source == "runtime-metadata":
        (example / "runtime.json").unlink()
        (example / "runtime.json").symlink_to(target)
    elif source == "containerfile":
        (example / "Containerfile").unlink()
        (example / "Containerfile").symlink_to(target)
    elif source == "credential-package":
        (credential / "src/credential_steps/leak.py").symlink_to(target)
    elif source == "credential-runtime-metadata":
        (credential / "runtime.json").unlink()
        (credential / "runtime.json").symlink_to(target)
    else:
        (credential / "Containerfile").unlink()
        (credential / "Containerfile").symlink_to(target)
    monkeypatch.setattr(helper, "ROOT", root)
    monkeypatch.setattr(helper, "EXAMPLE", example)
    monkeypatch.setattr(helper, "CONTRACTS", contracts)
    monkeypatch.setattr(helper, "CREDENTIAL_WORKLOAD", credential)
    commands = []
    monkeypatch.setattr(
        helper.subprocess, "run", lambda command, **kwargs: commands.append(command)
    )
    context = tmp_path / "context"

    if source.startswith("credential-"):
        with pytest.raises(ValueError, match="symlink"):
            helper.prepare_credential_workload(context)
    else:
        with pytest.raises(ValueError, match="symlink"):
            helper.prepare(context)

    assert commands == []
    assert not context.exists()


@pytest.mark.parametrize(
    "content",
    [
        "<testsuite/>",
        "<testsuite><testcase/><testcase/><testcase><skipped/></testcase></testsuite>",
        "<testsuite><testcase/><testcase/><testcase/><testcase/><testcase/></testsuite>",
    ],
)
def test_check_results_rejects_missing_or_skipped_cases(tmp_path, monkeypatch, content):
    helper = _load("check_results")
    result = tmp_path / "results.xml"
    result.write_text(content)
    monkeypatch.setattr(helper.sys, "argv", ["check_results.py", str(result)])
    with pytest.raises(SystemExit, match="7 executed"):
        helper.main()


def test_check_results_accepts_seven_executed_cases(tmp_path, monkeypatch):
    helper = _load("check_results")
    result = tmp_path / "results.xml"
    result.write_text(
        "<testsuite>"
        + "<testcase/>" * 7
        + "</testsuite>"
    )
    monkeypatch.setattr(helper.sys, "argv", ["check_results.py", str(result)])
    helper.main()


def test_runner_rejects_dirty_checkout_before_running_any_other_command(tmp_path):
    result, calls = _run_preflight(tmp_path, dirty=True, context="kind-integration-test")
    assert result.returncode != 0
    assert "clean" in result.stderr
    assert "kubectl" not in calls
    assert "uv" not in calls


def test_runner_requires_full_execution_plane_revision(tmp_path):
    result, calls = _run_preflight(
        tmp_path,
        dirty=False,
        context="kind-integration-test",
        revision="main",
    )
    assert result.returncode != 0
    assert "full 40-character commit SHA" in result.stderr
    assert calls == ""


def test_runner_requires_execution_plane_revision(tmp_path):
    result, calls = _run_preflight(
        tmp_path,
        dirty=False,
        context="kind-integration-test",
        revision=None,
    )
    assert result.returncode != 0
    assert "Set EP_REVISION" in result.stderr
    assert calls == ""


def test_runner_rejects_execution_plane_checkout_at_different_revision(tmp_path):
    result, calls = _run_preflight(
        tmp_path,
        dirty=False,
        context="kind-integration-test",
        revision="b" * 40,
    )
    assert result.returncode != 0
    assert "EP_PATH must be at" in result.stderr
    assert "kubectl" not in calls
    assert "uv" not in calls


def test_runner_rejects_wrong_context_without_collecting_diagnostics(tmp_path):
    result, calls = _run_preflight(tmp_path, dirty=False, context="other-cluster")
    assert result.returncode != 0
    assert "kind-integration-test" in result.stderr
    assert "kubectl get" not in calls
    assert "kubectl describe" not in calls
    assert "uv" not in calls


def test_runner_does_not_collect_diagnostics_if_context_changes(tmp_path):
    result, calls = _run_preflight(
        tmp_path, dirty=False, context="kind-integration-test", after_context="other-cluster"
    )
    assert result.returncode != 0
    assert "uv" in calls
    assert "kubectl --context kind-integration-test get" not in calls
    assert "kubectl --context kind-integration-test describe" not in calls


def test_runner_collects_diagnostics_only_from_expected_context(tmp_path):
    result, calls = _run_preflight(tmp_path, dirty=False, context="kind-integration-test")
    assert result.returncode != 0
    assert "kubectl --context kind-integration-test get pods" in calls


def test_egress_firewall_blocks_new_pod_connections(tmp_path):
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    firewall = tmp_path / "firewall"
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == "config current-context" ]]; then echo kind-integration-test; exit; fi\n'
        'echo "kubectl $*" >> "$CALLS"\n'
        'case "$*" in\n'
        '  *"iflink"*) echo 7;;\n'
        '  *"get node"*) echo 172.18.0.2;;\n'
        '  *"get service"*) echo 10.96.0.1;;\n'
        '  *"get pod"*) echo 10.244.0.8;;\n'
        '  *"nc -z"*) [[ ! -e "$FIREWALL" ]];;\n'
        "esac\n"
    )
    kubectl.chmod(0o755)
    engine = bin_dir / "engine"
    engine.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'echo "engine $*" >> "$CALLS"\n'
        'if [[ "$*" == *"ip -o link show"* ]]; then\n'
        '  echo "7: vethabc: <UP>"\n'
        '  for i in {1..10000}; do printf "dummy-interface-%s\\n" "$i"; done\n'
        'fi\n'
        'if [[ "$*" == *"iptables -I FORWARD"* ]]; then touch "$FIREWALL"; fi\n'
    )
    engine.chmod(0o755)
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CONTAINER_ENGINE": "engine",
        "CALLS": str(calls),
        "FIREWALL": str(firewall),
    }
    result = subprocess.run(
        ["bash", str(Path(__file__).with_name("restrict_egress.sh"))],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    commands = calls.read_text()
    assert (
        "-A COMPAT-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -j RETURN"
        in commands
    )
    assert "-A COMPAT-EGRESS -d 10.244.0.0/16 -j RETURN" not in commands
    assert commands.count("nc -z -w 3 10.244.0.8 80") == 2


def _run_preflight(tmp_path, *, dirty, context, after_context=None, revision="a" * 40):
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ep = tmp_path / "ep"
    ep.mkdir()
    calls = tmp_path / "calls"
    scripts = {
        "git": 'if [[ "$*" == *"rev-parse HEAD"* ]]; then echo "$ACTUAL_REVISION"; else [[ "$DIRTY" != 1 ]] || echo " M suspect.py"; fi',
        "kubectl": 'echo "kubectl $*" >> "$CALLS"; if [[ "$*" == "config current-context" ]]; then if [[ -e "$STATE" ]]; then echo "$AFTER_CONTEXT"; else touch "$STATE"; echo "$CONTEXT"; fi; fi',
        "uv": 'echo uv >> "$CALLS"; exit 1',
    }
    for name, script in scripts.items():
        command = bin_dir / name
        command.write_text(f"#!/usr/bin/env bash\n{script}\n")
        command.chmod(0o755)
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "EP_PATH": str(ep),
        "ACTUAL_REVISION": "a" * 40,
        "EP_TEST_DATABASE_URL": "postgresql://example",
        "EP_COMPAT_LOG_DIR": str(tmp_path / "logs"),
        "DIRTY": "1" if dirty else "0",
        "CONTEXT": context,
        "AFTER_CONTEXT": after_context or context,
        "CALLS": str(calls),
        "STATE": str(tmp_path / "state"),
    }
    env.pop("EP_REVISION", None)
    if revision is not None:
        env["EP_REVISION"] = revision
    result = subprocess.run(
        ["bash", str(Path(__file__).with_name("run_compat.sh"))],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, calls.read_text() if calls.exists() else ""


@pytest.mark.parametrize("failure", ["make", "later", "none"])
def test_runner_removes_generated_secrets_on_exit(tmp_path, failure):
    result, ep, secret_path = _run_with_fake_secrets(tmp_path, failure=failure)
    assert result.returncode == (0 if failure == "none" else 1), result.stderr
    assert secret_path.read_text() == ".secrets/ao-jwt-public.pem"
    assert not (ep / ".secrets").exists()


@pytest.mark.parametrize("failure", ["leak", "gate-leak"])
def test_runner_fails_when_the_sentinel_reaches_test_output(tmp_path, failure):
    result, ep, _ = _run_with_fake_secrets(tmp_path, failure=failure)
    assert result.returncode == 1, result.stderr
    assert "sentinel value appeared" in result.stderr
    # The value must be redacted from the collected files before upload.
    redacted = (tmp_path / "logs" / "results.xml").read_text(encoding="utf-8")
    assert "REDACTED-CREDENTIAL-SENTINEL" in redacted
    assert not (ep / ".secrets").exists()


def test_runner_removes_files_it_cannot_redact(tmp_path):
    result, ep, _ = _run_with_fake_secrets(tmp_path, failure="redact-fail")
    assert result.returncode == 1, result.stderr
    assert "sentinel value appeared" in result.stderr
    assert "unredactable" in result.stderr
    # A file whose redaction failed must not survive into the upload.
    assert not (tmp_path / "logs" / "results.xml").exists()
    assert not (ep / ".secrets").exists()


def test_runner_preserves_preexisting_secrets(tmp_path):
    result, ep, _ = _run_with_fake_secrets(tmp_path, failure="none", preexisting=True)
    assert result.returncode != 0
    assert (ep / ".secrets" / "existing").read_text() == "keep"


def _run_with_fake_secrets(tmp_path, *, failure, preexisting=False):
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ep = tmp_path / "ep"
    (ep / "tests/integration/kind").mkdir(parents=True)
    if preexisting:
        (ep / ".secrets").mkdir()
        (ep / ".secrets/existing").write_text("keep")
    secret_path = tmp_path / "key-path"
    scripts = {
        "git": 'if [[ "$*" == *"rev-parse HEAD"* ]]; then echo "$EP_REVISION"; fi',
        "kubectl": (
            'if [[ "$*" == "config current-context" ]]; then echo kind-integration-test; exit; fi\n'
            'case "$*" in\n'
            '  *"iflink"*) echo 7;;\n'
            '  *"get node"*) echo 172.18.0.2;;\n'
            '  *"get service"*) echo 10.96.0.1;;\n'
            '  *"get pod"*) echo 10.244.0.8;;\n'
            '  *"nc -z"*) [[ ! -e "$FIREWALL" ]];;\n'
            '  *"certificate-authority-data"*) echo Y2E=;;\n'
            "esac"
        ),
        "uv": (
            'if [[ "$*" == *pytest* ]]; then\n'
            "  echo '<testsuite/>' > \"$EP_COMPAT_LOG_DIR/results.xml\"\n"
            '  [[ "$FAILURE" != leak && "$FAILURE" != gate-leak && "$FAILURE" != redact-fail ]]'
            ' || echo -n "$EP_COMPAT_CREDENTIAL_TOKEN" >> "$EP_COMPAT_LOG_DIR/results.xml"\n'
            "fi\n"
            'if [[ "$FAILURE" == gate-leak && "$*" == *check_results* ]]; then exit 1; fi\n'
            "exit 0"
        ),
        "sed": (
            'if [[ "$FAILURE" == redact-fail && "$*" == *REDACTED-CREDENTIAL-SENTINEL* ]]; then exit 1; fi\n'
            'exec /usr/bin/sed "$@"'
        ),
        "make": (
            'printf %s "$EP_AO_JWT_PUBLIC_KEY_PATH" > "$KEY_PATH"\n'
            "mkdir -p .secrets\n"
            "echo private > .secrets/ao-jwt-private.pem\n"
            "echo public > .secrets/ao-jwt-public.pem\n"
            '[[ "$FAILURE" != make ]]'
        ),
        "engine": (
            'if [[ "$FAILURE" == later ]]; then exit 1; fi\n'
            'if [[ "$*" == "push "* ]]; then echo "compat: digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa size: 856"; exit; fi\n'
            'if [[ "$*" == "inspect "* ]]; then echo true; exit; fi\n'
            'if [[ "$*" == *"ip -o link show"* ]]; then echo "7: vethabc: <UP>"; fi\n'
            'if [[ "$*" == *"iptables -I FORWARD"* ]]; then touch "$FIREWALL"; fi\n'
            "exit 0"
        ),
    }
    for name, script in scripts.items():
        command = bin_dir / name
        command.write_text(f"#!/usr/bin/env bash\n{script}\n")
        command.chmod(0o755)
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "EP_PATH": str(ep),
        "EP_REVISION": "a" * 40,
        "EP_TEST_DATABASE_URL": "postgresql://example",
        "EP_COMPAT_LOG_DIR": str(tmp_path / "logs"),
        "CONTAINER_ENGINE": "engine",
        "KEY_PATH": str(secret_path),
        "FIREWALL": str(tmp_path / "firewall"),
        "FAILURE": failure,
    }
    env.pop("EP_AO_JWT_PUBLIC_KEY_PATH", None)
    result = subprocess.run(
        ["bash", str(Path(__file__).with_name("run_compat.sh"))],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, ep, secret_path
