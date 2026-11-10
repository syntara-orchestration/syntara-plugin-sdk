"""Reject tags and non-Quay references before provisioning the cluster."""

import os
import re

IMAGE = re.compile(r"quay\.io/[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}\Z")


def main() -> None:
    for name in ("EP_COMPAT_PYTHON_IMAGE", "EP_COMPAT_BASH_IMAGE", "EP_COMPAT_HTTP_IMAGE"):
        if not IMAGE.fullmatch(os.environ.get(name, "")):
            raise SystemExit(f"{name} must be a public Quay image pinned by sha256 digest")


if __name__ == "__main__":
    main()
