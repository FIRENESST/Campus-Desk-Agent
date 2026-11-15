"""Short-lived worker processes shared by the desktop writer and retrieval API."""

import json
import os
import subprocess
import sys
from pathlib import Path

from filelock import Timeout

if __package__:
    from .rag_storage import DEFAULT_URI, database_access_lock
else:
    from rag_storage import DEFAULT_URI, database_access_lock


class RagWorkerError(RuntimeError):
    pass


def run_worker(
    script: Path, payload: dict, *, timeout: float | None = None, lock_timeout: float = 30
) -> dict:
    interpreter = Path(sys.executable)
    if interpreter.name.lower() == "pythonw.exe":
        interpreter = interpreter.with_name("python.exe")

    uri = os.getenv("RAG_MILVUS_URI", str(DEFAULT_URI))
    # Lite holds its own storage lock until the child exits. Serialize cooperating
    # reads and writes around the whole child lifetime, not just the DB call.
    try:
        with database_access_lock(uri, timeout=lock_timeout):
            process = subprocess.run(
                [str(interpreter), "-B", "-X", "utf8", str(script.resolve()), "--worker"],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except Timeout:
        raise RagWorkerError("本地知识库正忙，请稍后重试") from None
    except subprocess.TimeoutExpired:
        raise RagWorkerError("知识库操作超时，请稍后重试") from None
    except OSError:
        raise RagWorkerError("无法访问本地知识库或启动工作进程") from None
    if process.returncode:
        raise RagWorkerError(f"知识库工作进程提前退出（{process.returncode}）")
    try:
        result = json.loads(process.stdout)
    except ValueError:
        raise RagWorkerError("知识库工作进程返回了无效结果") from None
    if not isinstance(result, dict):
        raise RagWorkerError("知识库工作进程返回了无效结果")
    return result
