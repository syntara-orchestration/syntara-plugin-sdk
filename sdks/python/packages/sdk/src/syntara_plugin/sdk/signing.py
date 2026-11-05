"""Small Cosign CLI adapter for signing and verifying immutable OCI references."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

_IMMUTABLE_OCI_REFERENCE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}$")
_COMMAND_TIMEOUT_SECONDS = 120


class CosignError(ValueError):
    """Raised when Cosign is missing or cannot sign/verify an OCI digest."""


def sign_oci_reference(reference: str, *, key: Path) -> None:
    """Attach a Cosign key signature to an immutable registry reference."""
    _validate_reference(reference)
    cosign = _cosign()
    key_path = key.expanduser().resolve()
    if not key_path.is_file():
        raise CosignError(f"Cosign signing key is not a regular file: {key_path}")
    _run(cosign, "sign", reference, key_path)


def verify_oci_reference(reference: str, *, key: Path) -> None:
    """Verify one immutable reference against the supplied Cosign public key."""
    _validate_reference(reference)
    cosign = _cosign()
    key_path = key.expanduser().resolve()
    if not key_path.is_file():
        raise CosignError(f"Cosign verification key is not a regular file: {key_path}")
    _run(cosign, "verify", reference, key_path)


def _validate_reference(reference: str) -> None:
    if _IMMUTABLE_OCI_REFERENCE.fullmatch(reference) is None:
        raise CosignError("Cosign requires an immutable repository@sha256:digest reference")


def _cosign() -> str:
    selected = shutil.which("cosign")
    if not selected:
        raise CosignError("Cosign is required for this signing or verification operation")
    return selected


def _run(cosign: str, action: Literal["sign", "verify"], reference: str, key: Path) -> None:
    """Run one fixed Cosign operation with validated values as individual arguments."""
    # Keep the allow-list check next to the subprocess sink as well as at the
    # public entry points. The reference is a positional CLI argument, so only
    # accept the restricted OCI repository@sha256:digest form here.
    _validate_reference(reference)
    if action == "sign":
        arguments = [cosign, "sign", "--yes", f"--key={key}", reference]
    elif action == "verify":
        arguments = [cosign, "verify", f"--key={key}", reference]
    else:  # Defensive guard for untyped internal callers.
        raise CosignError("Unsupported Cosign action")
    try:
        # `reference` is allow-listed and `key` is a resolved regular file;
        # fixed argv plus shell=False prevents either value from adding flags.
        result = subprocess.run(  # NOSONAR
            arguments,
            capture_output=True,
            check=False,
            shell=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CosignError(f"Cosign {action} command could not complete") from error
    if result.returncode != 0:
        # Registry responses can contain authorization details. Report a short
        # diagnostic without echoing command arguments, credentials, or output.
        raise CosignError(f"Cosign {action} failed with exit status {result.returncode}")
