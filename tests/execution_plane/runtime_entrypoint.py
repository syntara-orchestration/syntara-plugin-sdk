"""Start the secured SDK runtime behind the compatibility-only loopback bridge."""

from __future__ import annotations

import _thread
import subprocess
import threading


def _stop_when_runtime_exits(runtime: subprocess.Popen[object]) -> None:
    runtime.wait()
    _thread.interrupt_main()


def main() -> None:
    runtime = subprocess.Popen(["python3.12", "-m", "syntara_plugin.runtime.server"])
    threading.Thread(target=_stop_when_runtime_exits, args=(runtime,), daemon=True).start()
    try:
        from tls_bridge import serve

        serve()
    finally:
        runtime.terminate()
        runtime.wait()


if __name__ == "__main__":
    main()
