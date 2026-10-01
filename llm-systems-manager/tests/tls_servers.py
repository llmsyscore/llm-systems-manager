"""Local HTTPS servers with internal-CA certificates, for the role-check tests."""
from __future__ import annotations

import http.server
import ssl
import threading
from pathlib import Path

import _pki


class Pki:
    def __init__(self, d: Path):
        self.dir = Path(d)
        self.ca_cert, self.ca_key = _pki.load_or_create_ca(self.dir / "ca")
        self.ca_file = str(self.dir / "ca" / "internal-ca.crt")

    def leaf(self, label: str, role: str, agent_id: str):
        """Writes a 127.0.0.1 leaf of `role`; returns (crt_path, key_path)."""
        pem, key = _pki.sign_agent_cert(self.ca_cert, self.ca_key, agent_id=agent_id, hostname="hostx",
                                        ip_san="127.0.0.1", extra_dns_sans=["localhost"], role=role)
        crt, k = self.dir / f"{label}.crt", self.dir / f"{label}.key"
        crt.write_text(pem)
        k.write_text(key)
        return crt, k


class _Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class _QuietServer(http.server.ThreadingHTTPServer):
    """A client that rejects the certificate resets the connection; that is not worth a traceback."""

    def handle_error(self, request, client_address):
        pass


def serve(crt, key):
    """Starts an HTTPS server on an ephemeral 127.0.0.1 port; returns (base_url, stop)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv = _QuietServer(("127.0.0.1", 0), _Ok)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop():
        srv.shutdown()
        srv.server_close()
    return f"https://127.0.0.1:{srv.server_address[1]}", stop


def connect_proxy():
    """Starts a CONNECT-only proxy on an ephemeral 127.0.0.1 port; returns (proxy_url, stop)."""
    import select
    import socket
    import socketserver

    class Tunnel(socketserver.BaseRequestHandler):
        def handle(self):
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                head += chunk
            host, port = head.split(b" ")[1].decode().rsplit(":", 1)
            up = socket.create_connection((host, int(port)), timeout=5)
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            try:
                while True:
                    ready, _, _ = select.select([self.request, up], [], [], 10)
                    if not ready:
                        return
                    for s in ready:
                        buf = s.recv(65536)
                        if not buf:
                            return
                        (up if s is self.request else self.request).sendall(buf)
            finally:
                up.close()

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass

    srv = Server(("127.0.0.1", 0), Tunnel)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop():
        srv.shutdown()
        srv.server_close()
    return f"http://127.0.0.1:{srv.server_address[1]}", stop
