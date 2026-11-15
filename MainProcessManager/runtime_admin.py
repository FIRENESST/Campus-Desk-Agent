"""Allowlisted service actions dispatched by the supervised interface process."""

import secrets
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from .online_admin_store import admin_user_id
from .services import SIMULATIONS


MANAGED_BACKENDS = frozenset((*SIMULATIONS, "mintagw", "teacher-credentials", "rag-query", "mcpserver", "main-worker", "campus-rumor"))
ACTIONS = ("start", "stop", "restart")
ERRORS = (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.TimeoutExpired)


class RuntimeAdmin:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mpm-online-operation")
        self._lock = Lock()
        self._job = None
        self._closed = False

    def close(self):
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)

    def handle(self, pid, request):
        if not self.supervisor.owns_admin_caller(pid):
            return {"allowed": False}
        if request == {"operation": "status"}:
            with self._lock:
                job = dict(self._job) if self._job else None
            status = self.supervisor.status()
            items = [{"name": name, "status": state, "pid": process_id,
                      "actions": ACTIONS if name in MANAGED_BACKENDS else []}
                     for name, (state, process_id) in status.items()]
            return {"allowed": True, "items": items, "job": job}
        if request.keys() != {"operation", "service", "action", "actor"} or request["operation"] != "submit":
            return {"allowed": False}
        name, action, actor = (request[key] for key in ("service", "action", "actor"))
        if (not all(isinstance(value, str) for value in (name, action, actor)) or name not in MANAGED_BACKENDS
            or name not in self.supervisor.builtins or action not in ACTIONS):
            return {"allowed": False}
        try:
            if admin_user_id(actor) != actor:
                return {"allowed": False}
        except ValueError:
            return {"allowed": False}
        with self._lock:
            if self._closed or self._job and self._job["state"] in {"queued", "running"}:
                return {"allowed": True, "busy": True}
            job = {"id": secrets.token_hex(8), "service": name, "action": action, "state": "queued"}
            self._job = job
            self._pool.submit(self._run, job, "web:" + actor)
            return {"allowed": True, "job": dict(job)}

    def _run(self, job, actor):
        name, action = job["service"], job["action"]
        with self._lock:
            job["state"] = "running"
        try:
            with self.supervisor.command_lock:
                self.supervisor.store.record(actor, "admin.process." + action, name)
                if action == "stop":
                    self.supervisor.stop(actor, name)
                elif action == "restart" and self.supervisor.reload(actor, only=name):
                    pass
                else:
                    self.supervisor.start(actor, name)
        except ERRORS:
            with self._lock:
                job.update(state="failed", message="操作未完成，请查看主管理器日志或检查运行中的依赖方")
        else:
            with self._lock:
                job["state"] = "completed"
