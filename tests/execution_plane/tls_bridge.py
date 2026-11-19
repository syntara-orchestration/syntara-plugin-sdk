"""Plaintext-to-mTLS loopback bridge used only by the EP compatibility fixture."""

from __future__ import annotations

import socket
import ssl
import threading
import time
from pathlib import Path

# Keep in sync with the generated compat Containerfile: the certificates are
# staged under /opt/syntara because some build engines reserve /run/secrets.
_CERTS = Path("/opt/syntara/certificates")


def _copy(source: socket.socket, destination: socket.socket) -> None:
    try:
        while data := source.recv(65_536):
            destination.sendall(data)
    finally:
        destination.shutdown(socket.SHUT_WR)


def _runtime_connection(context: ssl.SSLContext) -> ssl.SSLSocket:
    deadline = time.monotonic() + 10
    while True:
        try:
            raw = socket.create_connection(("127.0.0.1", 50052), timeout=1)
            return context.wrap_socket(raw, server_hostname="localhost")
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def _handle(client: socket.socket, context: ssl.SSLContext) -> None:
    try:
        with client, _runtime_connection(context) as runtime:
            print("compatibility TLS bridge connected to runtime", flush=True)
            reverse = threading.Thread(target=_copy, args=(runtime, client), daemon=True)
            reverse.start()
            _copy(client, runtime)
            reverse.join()
    except OSError as error:
        print(f"compatibility TLS bridge failed: {error}", flush=True)


def serve() -> None:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=_CERTS / "client-ca.crt")
    context.load_cert_chain(_CERTS / "client.crt", _CERTS / "client.key")
    context.set_alpn_protocols(["h2"])
    with socket.create_server(("127.0.0.1", 50051), reuse_port=False) as listener:
        while True:
            client, _ = listener.accept()
            print("compatibility TLS bridge accepted client", flush=True)
            threading.Thread(target=_handle, args=(client, context), daemon=True).start()
