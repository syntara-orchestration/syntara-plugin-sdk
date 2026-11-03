"""Small Cosign CLI adapter for signing and verifying immutable OCI references."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

_IMMUTABLE_OCI_REFERENCE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}$")
_COMMAND_TIMEOUT_SECONDS = 120


class CosignError(ValueError):
    """Raised when Cosign is missing or cannot sign/verify an OCI digest."""


def sign_oci_reference(reference: str, *, key: Path, executable: str | None = None) -> None:
    """Attach a Cosign key signature to an immutable registry reference."""
    _validate_reference(reference)
    cosign = _cosign(executable)
    key_path = key.expanduser().resolve()
    if not key_path.is_file():
        raise CosignError(f"Cosign signing key is not a regular file: {key_path}")
    _run([cosign, "sign", "--yes", "--key", str(key_path), reference], action="sign")


def verify_oci_reference(reference: str, *, key: Path, executable: str | None = None) -> None:
    """Verify one immutable reference against the supplied Cosign public key."""
    _validate_reference(reference)
    cosign = _cosign(executable)
    key_path = key.expanduser().resolve()
    if not key_path.is_file():
        raise CosignError(f"Cosign verification key is not a regular file: {key_path}")
    _run([cosign, "verify", "--key", str(key_path), reference], action="verify")


def _validate_reference(reference: str) -> None:
    if _IMMUTABLE_OCI_REFERENCE.fullmatch(reference) is None:
        raise CosignError("Cosign requires an immutable repository@sha256:digest reference")


def _cosign(executable: str | None) -> str:
    selected = executable or shutil.which("cosign")
    if not selected:
        raise CosignError("Cosign is required for this signing or verification operation")
    return selected


def _run(command: list[str], *, action: str) -> None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CosignError(f"Cosign {action} command could not complete") from error
    if result.returncode != 0:
        # Registry responses can contain authorization details. Report a short
        # diagnostic without echoing command arguments, credentials, or output.
        raise CosignError(f"Cosign {action} failed with exit status {result.returncode}")
