"""使用真实本地端口验证启动和流式转发，无需外部网络或真实 API Key。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

PROJECT_DIR = Path(__file__).resolve().parents[1]


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if (
            self.path != "/api/v1/chat/completions"
            or self.headers.get("authorization") != "Bearer smoke-upstream-key"
            or body.get("model") != "smoke-model"
        ):
            self.send_error(400)
            return
        if body.get("stream"):
            chunks = (b'data: {"delta":"first"}\n\n', b"data: [DONE]\n\n")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(sum(map(len, chunks))))
            self.send_header("connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(chunks[0])
            self.wfile.flush()
            time.sleep(0.5)
            self.wfile.write(chunks[1])
            self.wfile.flush()
        else:
            content = b'{"choices":[{"message":{"content":"smoke-ok"}}]}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(content)))
            self.send_header("connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(content)


def main():
    # 先确认端口可用；绝不停止其他已在运行的服务。
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 8000))
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    env = os.environ.copy()
    env.update({
        "LLM_API_KEY": "smoke-upstream-key",
        "LLM_BASE_URL": f"http://127.0.0.1:{upstream.server_port}/api/v1",
        "LLM_MODEL": "smoke-model",
        "LOCAL_PORT": "8000",
        "LOCAL_API_KEY": "smoke-local-key",
        "LLM_TIMEOUT": "5",
        "NO_PROXY": "127.0.0.1,localhost",
    })
    process = None
    try:
        process = subprocess.Popen([sys.executable, "server.py"], cwd=PROJECT_DIR, env=env)
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=5, trust_env=False) as client:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise RuntimeError("转发服务启动失败")
                try:
                    client.get("/health").raise_for_status()
                    break
                except httpx.RequestError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("等待转发服务启动超时") from None
                    time.sleep(0.1)

            headers = {"authorization": "Bearer smoke-local-key"}
            payload = {"messages": [{"role": "user", "content": "test"}]}
            response = client.post("/v1/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            assert response.json()["choices"][0]["message"]["content"] == "smoke-ok"
            assert client.post("/v1/chat/completions", json=payload).status_code == 401
            with client.stream(
                "POST", "/v1/chat/completions", json=payload | {"stream": True}, headers=headers
            ) as response:
                response.raise_for_status()
                events = []
                for line in response.iter_lines():
                    if line:
                        events.append((line, time.monotonic()))
                assert [line for line, _ in events] == ['data: {"delta":"first"}', "data: [DONE]"]
                assert events[1][1] - events[0][1] > 0.25, "流式响应被缓冲了"
            print("SMOKE OK: 127.0.0.1:8000 / JSON / local authentication / incremental SSE")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()


if __name__ == "__main__":
    main()
