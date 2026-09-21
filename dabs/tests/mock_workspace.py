"""A tiny localhost stand-in for a Databricks workspace, so `databricks bundle validate`
can run FULLY OFFLINE — no cloud, no real credentials, no internet.

Why this exists: even with a placeholder host and a dummy token, the Databricks CLI resolves
the current user during `bundle validate` by calling `GET /api/2.0/preview/scim/v2/Me`. With a
non-resolvable host that call fails and validate exits non-zero. This server answers that one
endpoint (and a permissive catch-all) with canned JSON on 127.0.0.1, so validation exercises the
real bundle schema/config path without ever leaving the machine.

Usage:
    python -m dabs.tests.mock_workspace          # prints "PORT=<port>" then serves forever
    python -m dabs.tests.mock_workspace --port 8123

Nothing here is a secret or a real workspace value — it is a loopback fixture.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ME = {
    "id": "0",
    "userName": "ci-offline@example.invalid",
    "displayName": "CI Offline Validator",
    "active": True,
}


class _Handler(BaseHTTPRequestHandler):
    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path.startswith("/api/2.0/preview/scim/v2/Me"):
            self._send(_ME)
        elif self.path.startswith("/api/2.0/workspace/get-status"):
            # `bundle validate` (files_to_sync mutator) stats the deployment root and requires it
            # to be a DIRECTORY; without object_type it errors "points to a <empty>".
            self._send({"object_type": "DIRECTORY", "path": "/Workspace", "object_id": 1})
        else:
            # Permissive catch-all: any other read the CLI attempts gets a benign empty object.
            self._send({})

    def do_POST(self) -> None:  # noqa: N802
        self._send({})

    def log_message(self, *_args) -> None:  # silence per-request logging
        return


def serve(port: int = 0) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    bound_port = httpd.server_address[1]
    # Emit the port on stdout so a caller (test/CI) can discover an ephemeral port.
    print(f"PORT={bound_port}", flush=True)
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0, help="Port to bind (0 = ephemeral).")
    args = parser.parse_args(argv)
    serve(args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
