"""Offline integration: real LiteLLM proxy, local fake provider, no live model."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model = data.get("model", "gpt-4o")
        if self.path.endswith("/responses"):
            body = {"id": "resp_offline", "object": "response", "created_at": int(time.time()),
                    "status": "completed", "model": model,
                    "output": [{"id": "msg_offline", "type": "message", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        else:
            body = {"id": "chatcmpl-offline", "object": "chat.completion", "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        if data.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in (({"role": "assistant", "content": "ok"}, None), ({}, "stop")):
                chunk = {"id": "chatcmpl-offline", "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    provider = ThreadingHTTPServer(("127.0.0.1", 18731), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="gateway-proxy-proof-") as directory:
        root = Path(directory)
        config = root / "config.yaml"
        # The proxy loads custom modules relative to its configuration file.
        # Match the two sibling ConfigMap mounts used by the deployment.
        shutil.copyfile("/probe-mod/gateway_audit.py", root / "gateway_audit.py")
        config.write_text("""model_list:
  - model_name: probe-alias
    litellm_params:
      model: openai/gpt-4o
      api_base: http://127.0.0.1:18731/v1
      api_key: os.environ/PROBE_UPSTREAM_KEY
litellm_settings:
  callbacks: [gateway_audit.logger_instance]
  num_retries: 0
  set_verbose: false
general_settings:
  master_key: os.environ/PROBE_MASTER_KEY
""")
        audit = root / "audit"
        log = root / "proxy.log"
        env = {**os.environ, "PYTHONPATH": "/probe-mod", "HOME": str(root),
               "PROBE_UPSTREAM_KEY": "offline-provider-placeholder", "PROBE_MASTER_KEY": "sk-offline-placeholder",
               "STAGE2_LITELLM_CONFIG_FILE": str(config), "RESBENCH_GATEWAY_AUDIT_DIR": str(audit),
               "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
        with log.open("wb") as output:
            process = subprocess.Popen(["litellm", "--config", str(config), "--host", "127.0.0.1", "--port", "18732"],
                                       env=env, stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("proxy exited before readiness")
                    try:
                        with urlopen("http://127.0.0.1:18732/health/liveliness", timeout=1):
                            break
                    except (URLError, TimeoutError):
                        time.sleep(0.1)
                else:
                    raise RuntimeError("proxy readiness timeout")
                routes = [
                    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "DO_NOT_LOG_PROMPT"}]}),
                    ("/v1/chat/completions", {"stream": True, "messages": [{"role": "user", "content": "DO_NOT_LOG_PROMPT"}]}),
                    ("/v1/responses", {"input": "DO_NOT_LOG_PROMPT", "max_output_tokens": 10}),
                    ("/v1/messages", {"max_tokens": 10, "messages": [{"role": "user", "content": "DO_NOT_LOG_PROMPT"}]}),
                ]
                statuses = []
                harnesses = ["codex", "claude-code", "deepseek-harness", "bladeai"]
                for index, (path, payload) in enumerate(routes * len(harnesses)):
                    headers = {"Authorization": "Bearer sk-offline-placeholder", "Content-Type": "application/json",
                               "anthropic-version": "2023-06-01", "x-resbench-trial-id": "qualification-offline",
                               "x-resbench-harness": harnesses[index // len(routes)], "x-resbench-model-alias": "probe-alias",
                               "x-resbench-request-id": f"offline-{index}", "x-resbench-gateway-config-sha256": "wrong"}
                    request = Request("http://127.0.0.1:18732" + path, data=json.dumps({"model": "probe-alias", **payload}).encode(), headers=headers)
                    try:
                        with urlopen(request, timeout=20) as response:
                            response.read()
                            statuses.append({"path": path, "status": response.status})
                    except HTTPError as error:
                        statuses.append({"path": path, "status": error.code})
                path = audit / "qualification-offline.jsonl"
                raw = path.read_text() if path.exists() else ""
                rows = [json.loads(line) for line in raw.splitlines()]
                expected_version = hashlib.sha256(config.read_bytes()).hexdigest()
                passed = (len(rows) == 16 and {row["request_id"] for row in rows} == {f"offline-{n}" for n in range(16)}
                          and all(value["status"] == 200 for value in statuses)
                          and {row["harness"] for row in rows} == set(harnesses)
                          and all(row["gateway_config_sha256"] == expected_version and row["outcome"] == "received" for row in rows)
                          and "DO_NOT_LOG_PROMPT" not in raw and "placeholder" not in raw)
                print(json.dumps({"mode": "offline_proxy_fake_provider", "statuses": statuses, "receipts": rows,
                                  "passed": passed, "live_model_called": False}))
                if not passed:
                    print(log.read_text(errors="replace")[:6000])
                    print(log.read_text(errors="replace")[-8000:])
                return 0 if passed else 1
            except Exception:
                print(log.read_text(errors="replace")[:6000])
                print(log.read_text(errors="replace")[-8000:])
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                provider.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
