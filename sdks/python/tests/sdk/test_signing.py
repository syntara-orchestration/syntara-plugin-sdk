from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from syntara_plugin.sdk import signing


@pytest.mark.parametrize(
    ("operation", "expected_arguments"),
    [
        ("sign", ["/tools/cosign", "sign", "--yes"]),
        ("verify", ["/tools/cosign", "verify"]),
    ],
)
def test_cosign_uses_fixed_operations_and_passes_key_as_one_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    expected_arguments: list[str],
) -> None:
    key = tmp_path / "key with spaces.pem"
    key.write_text("key", encoding="utf-8")
    reference = "registry.example/acme/plugin@sha256:" + "a" * 64
    captured: dict[str, Any] = {}

    monkeypatch.setattr(signing.shutil, "which", lambda name: "/tools/cosign")

    def fake_run(arguments: list[str], **options: Any) -> Any:
        captured["arguments"] = arguments
        captured["options"] = options
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(signing.subprocess, "run", fake_run)
    if operation == "sign":
        signing.sign_oci_reference(reference, key=key)
    else:
        signing.verify_oci_reference(reference, key=key)

    assert captured["arguments"] == [
        *expected_arguments,
        f"--key={key.resolve()}",
        reference,
    ]
    assert captured["options"]["shell"] is False


def test_signing_rejects_non_digest_references_before_invoking_cosign(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    invoked = False

    def fake_run(*_arguments: Any, **_options: Any) -> Any:
        nonlocal invoked
        invoked = True
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(signing.shutil, "which", lambda name: "/tools/cosign")
    monkeypatch.setattr(signing.subprocess, "run", fake_run)
    key = tmp_path / "key.pem"
    key.write_text("key", encoding="utf-8")

    with pytest.raises(signing.CosignError, match="immutable"):
        signing.sign_oci_reference("--help", key=key)

    assert not invoked
