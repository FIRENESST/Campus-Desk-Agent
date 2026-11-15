"""Start the local RAG database, HTTP service, and desktop writer together."""

import os
import subprocess
import sys
import time
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

import httpx2
from filelock import Timeout

if __package__:
    from .rag_api import API_HOST
    from .rag_db import RagError
    from .rag_worker import database_access_lock
else:
    from rag_api import API_HOST
    from rag_db import RagError
    from rag_worker import database_access_lock


RAG_DIR = Path(__file__).resolve().parent


def _read_api_port(process: subprocess.Popen) -> int:
    line = Queue(maxsize=1)
    Thread(target=lambda: line.put(process.stdout.readline()), daemon=True).start()
    try:
        return int(line.get(timeout=15))
    except (Empty, ValueError):
        raise RagError("查询服务未能启动，请检查端口或启动输出") from None


def _wait_for_api(process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 20
    with httpx2.Client(timeout=1, trust_env=False) as client:
        while process.poll() is None and time.monotonic() < deadline:
            try:
                response = client.get(f"http://{API_HOST}:{port}/health")
                if response.status_code == 200 and response.json() == {"status": "ready"}:
                    return
            except (httpx2.RequestError, ValueError):
                pass
            time.sleep(0.15)
    raise RagError("查询服务未能启动，请检查端口和本地模型")


def _stop_api(process: subprocess.Popen) -> None:
    process.stdin.close()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait()


def start_rag(uri: str, collection: str, port: int | None) -> int:
    if port is not None and not 1 <= port <= 65535:
        raise RagError("查询服务端口需在 1 到 65535 之间")

    if "://" not in uri:
        uri = str(Path(uri).resolve())
    env = dict(os.environ, RAG_MILVUS_URI=uri, RAG_COLLECTION=collection)
    interpreter = Path(sys.executable)
    command = [str(interpreter), "-B", "-X", "utf8"]
    options = {"cwd": RAG_DIR, "env": env}
    try:
        with database_access_lock(uri):
            init = subprocess.run([*command, "rag_db.py", "init"], **options)
        if init.returncode:
            return init.returncode

        api_command = [*command, "rag_api.py", "--managed"]
        if port is not None:
            api_command.extend(("--port", str(port)))
        with subprocess.Popen(
            api_command, **options, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ) as api:
            try:
                actual_port = _read_api_port(api)
                _wait_for_api(api, actual_port)
                url = f"http://{API_HOST}:{actual_port}/search"
                writer = interpreter.with_name("pythonw.exe") if os.name == "nt" else interpreter
                window = subprocess.Popen(
                    [str(writer), "-B", "rag_writer.py"], cwd=RAG_DIR, env=dict(env, RAG_QUERY_URL=url),
                )
                print(f"查询服务 {url}，关闭资料管理窗口后结束服务", flush=True)
                return window.wait()
            finally:
                _stop_api(api)
    except Timeout:
        raise RagError("本地知识库正忙，请稍后启动") from None
    except KeyboardInterrupt:
        return 0
