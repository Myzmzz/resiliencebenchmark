#!/usr/bin/env python3
"""Local-only HTTP bridge from Postman to the remote Stage2 cluster service."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


REMOTE_SCRIPT = r"""
import base64
import json
import sys
import urllib.error
import urllib.request

envelope = json.load(sys.stdin)
request = urllib.request.Request(
    envelope["base_url"].rstrip("/") + envelope["path"],
    data=base64.b64decode(envelope["body_b64"]) if envelope["body_b64"] else None,
    headers=envelope["headers"],
    method=envelope["method"],
)
try:
    with urllib.request.urlopen(request, timeout=1800) as response:
        status = response.status
        headers = dict(response.headers.items())
        body = response.read()
except urllib.error.HTTPError as error:
    status = error.code
    headers = dict(error.headers.items())
    body = error.read()
json.dump(
    {
        "status": status,
        "headers": headers,
        "body_b64": base64.b64encode(body).decode("ascii"),
    },
    sys.stdout,
)
"""


class BridgeServer(ThreadingHTTPServer):
    remote_host: str
    remote_url: str


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeServer

    def do_GET(self):  # noqa: N802
        self._forward()

    def do_POST(self):  # noqa: N802
        self._forward()

    def do_PUT(self):  # noqa: N802
        self._forward()

    def do_DELETE(self):  # noqa: N802
        self._forward()

    def _forward(self) -> None:
        if not self.path.startswith(("/api/", "/healthz", "/openapi.json")):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower()
            in {
                "accept",
                "content-type",
                "idempotency-key",
            }
        }
        envelope = {
            "base_url": self.server.remote_url,
            "path": self.path,
            "method": self.command,
            "headers": headers,
            "body_b64": base64.b64encode(body).decode("ascii"),
        }
        command = "python3 -c " + shlex.quote(REMOTE_SCRIPT)
        completed = subprocess.run(
            [
                "sshpass",
                "-e",
                "ssh",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "ConnectTimeout=15",
                f"root@{self.server.remote_host}",
                command,
            ],
            input=json.dumps(envelope).encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=1830,
        )
        if completed.returncode:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "error": {
                            "code": "REMOTE_STAGE2_UNAVAILABLE",
                            "message": "the local Postman bridge could not reach Stage2",
                        }
                    }
                ).encode("utf-8")
            )
            return
        try:
            response: dict[str, Any] = json.loads(completed.stdout)
            response_body = base64.b64decode(response["body_b64"])
        except (KeyError, ValueError, json.JSONDecodeError):
            self.send_error(502)
            return
        self.send_response(int(response["status"]))
        content_type = response.get("headers", {}).get(
            "Content-Type", "application/json"
        )
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=18088)
    parser.add_argument("--remote-host", default="1.94.151.57")
    parser.add_argument("--remote-url", default="http://10.96.2.104:8080")
    args = parser.parse_args()
    if not os.environ.get("SSHPASS"):
        raise RuntimeError("SSHPASS must be provided at runtime")
    server = BridgeServer((args.listen_host, args.listen_port), BridgeHandler)
    server.remote_host = args.remote_host
    server.remote_url = args.remote_url
    print(
        f"Stage2 Postman bridge listening on "
        f"http://{args.listen_host}:{args.listen_port}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
