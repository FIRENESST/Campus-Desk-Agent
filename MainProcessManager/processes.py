"""Own and supervise only the processes started by this local session."""

import json
import os
import secrets
import sqlite3
import subprocess
import time
from dataclasses import asdict
from graphlib import TopologicalSorter
from pathlib import Path
from threading import RLock

from server.CASserver.store import STATE_SUBDIR as CAS_STATE_SUBDIR

from .external import FRP_BACKEND, FRP_SERVICES, LAN_BACKEND, LAN_SERVICES, ExternalFrp, ExternalWebsite
from .services import RELOAD_APPS, custom_service, project_services
from .winjob import ProcessJob, listening_pids


READY_STATES = {"ready", "running", "external"}
CONTROL_ENV = {"MPM_AUTH_PIPE", "MPM_AUTH_PID", "MPM_WEB_ADMIN_BRIDGE", "MPM_WEB_ADMIN_STATE",
               "MPM_RUNTIME_STATUS_KEY", "MPM_RUMOR_BRIDGE_KEY"}
INTERFACE_CALLERS = ("main-agent", "main-worker", "unified-interface")
PUBLISHED_SSO_SERVICES = ("cas-server", "main-agent", "main-worker")
PUBLICATION_SERVICES = {"website": LAN_SERVICES, "frp": FRP_SERVICES}
PROCESS_ERRORS = (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.TimeoutExpired)


class Supervisor:
    def __init__(self, store, root: Path, *, services=None):
        self.store = store
        self.root = root
        self.builtins = dict(project_services(root) if services is None else services)
        self.services = self.builtins | {
            name: custom_service(name, definition, root) for name, definition in store.registrations().items()
        }
        self.logs = store.directory / "logs"
        self.logs.mkdir(exist_ok=True)
        self._lock = RLock()
        self.command_lock = RLock()
        self._jobs = {}
        self._exits = {}
        self._reload_pending = set()
        self.control_pipe = None
        self._admin_bridge = secrets.token_urlsafe(32)
        self._status_key = secrets.token_urlsafe(32)
        self._rumor_bridge_key = secrets.token_urlsafe(32)
        self._publications = {}
        self._publication_needs_reload = False
        for name, publication_type in (("website", ExternalWebsite), ("frp", ExternalFrp)):
            configuration = store.external_config(name)
            if configuration is not None:
                self._configure_publication(None, name, publication_type(**configuration))

    @property
    def external_website(self):
        return self._publications.get("website")

    def log_path(self, name: str):
        self._service(name)
        return self.logs / f"{name}.log"

    def _service(self, name):
        if name not in self.services:
            raise ValueError("进程未登记，使用 status 查看登记表")
        return self.services[name]

    def register(self, actor, name, definition):
        if name in self.services:
            raise ValueError("进程名已登记")
        service = custom_service(name, definition, self.root)
        if set(service.depends_on) - self.services.keys():
            raise ValueError("请先登记所有依赖进程")
        if any(not self.services[dependency].start_all for dependency in service.depends_on):
            raise ValueError("对外服务进程需要单独启动，不能作为自定义进程的依赖")
        self.store.register(actor, name, definition)
        with self._lock:
            self.services[name] = service

    def unregister(self, actor, name):
        self._service(name)
        if name in self.builtins:
            raise ValueError("项目预设保留在进程登记表中")
        if self._job(name) is not None or any(name in item.depends_on for item in self.services.values()):
            raise ValueError("请先停止此进程并解除其他登记项的依赖")
        self.store.unregister(actor, name)
        with self._lock:
            del self.services[name]
            self._reload_pending.discard(name)

    def configure_external_website(self, actor, publication: ExternalWebsite):
        self._configure_publication(actor, "website", publication)

    def configure_external_frp(self, actor, publication: ExternalFrp):
        self._configure_publication(actor, "frp", publication)

    def _configure_publication(self, actor, name, publication):
        with self._lock:
            process_names = PUBLICATION_SERVICES[name]
            previous = self._publications.get(name)
            if previous is None and any(process in self.services for process in process_names):
                raise ValueError("对外服务进程名已被占用，请检查已有的自定义登记")
            if any(self._job(process) is not None for process in process_names):
                raise ValueError(f"请先运行 external stop {name}，再修改配置")
            other = self._publications.get("frp" if name == "website" else "website")
            if other and {publication.origin, publication.cas_origin} & {other.origin, other.cas_origin}:
                raise ValueError("局域网和 FRP 的校园助手与 CAS 源地址须互不相同")
            lan = publication if name == "website" else other
            frp = publication if name == "frp" else other
            if lan and frp and lan.backend_port in {frp.backend_port, frp.website_port, frp.cas_port}:
                raise ValueError("局域网网站后端端口与 FRP 本机端口冲突")
            services = publication.processes(self._service("user-website"), self.root, self.store.directory)
            if actor is not None:
                self.store.configure_external(actor, name, asdict(publication))
                if publication != previous and any(self._job(service) is not None for service in PUBLISHED_SSO_SERVICES):
                    self._publication_needs_reload = True
            self.builtins.update(services)
            self.services.update(services)
            self._publications[name] = publication

    def require_external_website(self):
        if self.external_website is None:
            raise ValueError("网站局域网入口尚未配置，请使用 external configure website 内网IPv4")
        return self.external_website

    def start_external_website(self, actor):
        self.require_external_website()
        return self._start_publication(actor, "website")

    def require_external_frp(self):
        if "frp" not in self._publications:
            raise ValueError("请先使用 external configure frp 保存两个 HTTPS 源地址")
        return self._publications["frp"]

    def start_external_frp(self, actor):
        self.require_external_frp()
        return self._start_publication(actor, "frp")

    def _start_publication(self, actor, name):
        proxy, backend = PUBLICATION_SERVICES[name]
        if self._job(proxy) is None:
            try:
                self._service(proxy).command()
            except ValueError as exc:
                raise ValueError("找不到 Caddy，请将 caddy.exe 放入 MainProcessManager 或加入 PATH") from exc
        try:
            if self._publication_needs_reload:
                self.reload(actor, only=PUBLISHED_SSO_SERVICES)
                self._publication_needs_reload = False
            # Starting an already running proxy should also recover a backend that exited.
            if self._job(proxy) is not None:
                self.start(actor, backend)
            return self.start(actor, proxy)
        except PROCESS_ERRORS + (KeyboardInterrupt,) as exc:
            try:
                self._stop_publication(actor, name)
            except PROCESS_ERRORS as cleanup:
                raise RuntimeError(f"{exc}\n停止对外入口失败（{cleanup}），请查看状态和日志") from cleanup
            raise

    def stop_external_website(self, actor):
        self.require_external_website()
        self._stop_publication(actor, "website")

    def stop_external_frp(self, actor):
        self.require_external_frp()
        self._stop_publication(actor, "frp")

    def _stop_publication(self, actor, name):
        for process in PUBLICATION_SERVICES[name]:
            self.stop(actor, process)

    def _job(self, name):
        with self._lock:
            job = self._jobs.get(name)
            if job is not None and not job.active_count():
                self._forget(name, job)
                return None
            return job

    def _forget(self, name, job, *, stopped=False):
        with self._lock:
            if stopped:
                self._exits.pop(name, None)
            else:
                self._exits[name] = job.wait_root()
            del self._jobs[name]
            job.close()

    def ready_services(self) -> set[str]:
        owners = listening_pids()
        with self._lock:
            return {
                name for name, job in self._jobs.items()
                if self.services[name].listener and bool(job.pids() & owners)
            }

    def owns_interface_caller(self, pid: int) -> bool:
        with self._lock:
            return any(self._jobs[name].contains(pid) for name in INTERFACE_CALLERS if name in self._jobs)

    def owns_admin_caller(self, pid: int) -> bool:
        if pid not in listening_pids():
            return False
        with self._lock:
            job = self._jobs.get("unified-interface")
            # The Windows venv launcher and the HTTP listener can have different PIDs.
            return bool(job and job.contains(pid))

    def _status(self, name: str, ready: set[str]):
        spec = self._service(name)
        job = self._job(name)
        if job is not None:
            if spec.postgres_data is not None:
                pid = self._postgres_pid(spec)
                ready_db = job.wait_root() == 0 and pid is not None and job.contains(pid)
                return ("ready", pid) if ready_db else ("starting", job.pid)
            if spec.listener:
                return ("ready" if name in ready else "starting"), job.pid
            return "running", job.pid
        if spec.postgres_data is not None:
            pid = self._postgres_pid(spec)
            if pid is not None and self._external_postgres(spec):
                return "external", pid
        return ("exited" if name in self._exits else "stopped"), None

    def status(self, name=None):
        ready = self.ready_services()
        return {item: self._status(item, ready) for item in ((name,) if name else self.services)}

    @staticmethod
    def _postgres_pid(spec):
        try:
            with (spec.postgres_data / "postmaster.pid").open("rt", encoding="ascii") as handle:
                pid = int(handle.readline(32))
            return pid if 0 < pid <= 0xffffffff else None
        except (OSError, ValueError):
            return None

    def _external_postgres(self, spec):
        try:
            return self._pg_ctl(spec, "status", timeout=5) == 0
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False

    def _pg_ctl(self, spec, *args, timeout):
        with self.log_path(spec.name).open("ab") as log:
            return subprocess.run(
                [*spec.command(), "-D", str(spec.postgres_data), *args],
                cwd=spec.cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW,
            ).returncode

    def _command_and_env(self, spec):
        argv = spec.command()
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        env.update(spec.env)
        for name in CONTROL_ENV:
            env.pop(name, None)
        if spec.name in PUBLISHED_SSO_SERVICES and self._publications:
            sites = {publication.origin: publication.cas_origin for publication in self._publications.values()}
            name = "CAS_PUBLIC_SITES" if spec.name == "cas-server" else "CLOUD_CAS_PUBLIC_SITES"
            env[name] = json.dumps(sites)
        if spec.name in {"main-agent", "main-worker"}:
            config = spec.cwd / ".env.cloud"
            if config.is_file():
                argv.extend(("--env-file", str(config)))
            elif not env.get("CLOUD_DATABASE_URL"):
                raise ValueError("请先配置 server/MainAgent/.env.cloud 或 CLOUD_DATABASE_URL")
        if spec.name in INTERFACE_CALLERS and self.control_pipe is not None:
            env["MPM_AUTH_PIPE"] = self.control_pipe
            env["MPM_AUTH_PID"] = str(os.getpid())
        if self.control_pipe is not None and spec.name in {"unified-interface", "user-website", LAN_BACKEND, FRP_BACKEND}:
            env["MPM_WEB_ADMIN_BRIDGE"] = self._admin_bridge
        if self.control_pipe is not None and spec.name == "unified-interface":
            env["MPM_WEB_ADMIN_STATE"] = str(self.store.directory / "online-admin")
        if self.control_pipe is not None and spec.name in {"unified-interface", "campus-rumor"}:
            env["MPM_RUMOR_BRIDGE_KEY"] = self._rumor_bridge_key
        if self.control_pipe is not None and spec.name in {
            "unified-interface", "main-agent", "main-worker", "cas-server", "mcpserver", "campus-digest",
        }:
            env["MPM_RUNTIME_STATUS_KEY"] = self._status_key
        if spec.name == "main-worker":
            env["CLOUD_WORKERS"] = "1"
        if spec.name == "cas-server":
            env["CAS_DATA_DIR"] = str(self.store.directory / CAS_STATE_SUBDIR)
            cloud_config = self.root / "server" / "MainAgent" / ".env.cloud"
            if cloud_config.is_file():
                env.setdefault("CAS_WEBSITE_ENV_FILE", str(cloud_config))
        if spec.postgres_data is not None:
            if not (spec.postgres_data / "PG_VERSION").is_file():
                raise ValueError("PostgreSQL 数据目录尚未初始化")
            argv.extend(("-D", str(spec.postgres_data), "-l", str(self.logs / "postgresql-server.log"),
                         "-w", "-t", "45", "start"))
        return argv, env

    def start(self, actor, name):
        spec = self._service(name)
        job = self._job(name)
        if job is None:
            if spec.postgres_data is not None and self._postgres_pid(spec) and self._external_postgres(spec):
                self._reload_pending.discard(name)
                return "external"
            argv, env = self._command_and_env(spec)
            for dependency in spec.depends_on:
                self.start(actor, dependency)
            with self._lock:
                job = ProcessJob.start(argv, spec.cwd, env, self.log_path(name))
                self._jobs[name] = job
            self.store.record(actor, "process.start", name)
        status = self._wait_ready(spec, job)
        self._reload_pending.discard(name)
        return status

    def _wait_ready(self, spec, job):
        if spec.postgres_data is not None and job.wait_root(50) not in {0, None}:
            self._job(spec.name)
            raise RuntimeError(f"{spec.name} 启动失败，请查看日志")
        deadline = time.monotonic() + 30
        while True:
            status, _ = self._status(spec.name, self.ready_services())
            if status in READY_STATES:
                return status
            if status == "exited":
                if not spec.listener and self._exits[spec.name] == 0:
                    return "completed"
                raise RuntimeError(f"{spec.name} 已退出，退出码 {self._exits[spec.name]}，请查看日志")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{spec.name} 尚未就绪，请查看日志或在本机终端停止")
            time.sleep(0.1)

    def _depends_on(self, service, name):
        return name in service.depends_on or any(self._depends_on(self.services[dependency], name)
                                                 for dependency in service.depends_on)

    def _cancel_reload(self, name):
        self._reload_pending.difference_update({
            item for item in self._reload_pending
            if item == name or self._depends_on(self.services[item], name)
        })

    def stop(self, actor, name):
        spec = self._service(name)
        job = self._job(name)
        if job is None:
            if spec.postgres_data is not None and self._postgres_pid(spec) and self._external_postgres(spec):
                raise RuntimeError("当前 PostgreSQL 为外部运行的实例，请使用它原有的管理入口")
            self._cancel_reload(name)
            return
        dependents = [item for item in list(self._jobs) if self._depends_on(self.services[item], name) and self._job(item)]
        if dependents:
            raise RuntimeError("请先停止依赖它的进程 " + "、".join(dependents))
        if spec.postgres_data is not None:
            pid = self._postgres_pid(spec)
            if pid is None or not job.contains(pid):
                if not job.wait_empty(1):
                    raise RuntimeError("无法确认当前 PostgreSQL PID 属于主管理器")
            elif self._pg_ctl(spec, "-m", "fast", "-w", "-t", "30", "stop", timeout=35) != 0 or not job.wait_empty(5):
                raise RuntimeError("PostgreSQL 尚未安全停止，请查看日志")
        elif spec.window:
            if not job.request_window_close() and not job.wait_empty(1):
                raise RuntimeError("请在资料管理窗口关闭 RAG 写入程序")
            if not job.wait_empty(30):
                raise RuntimeError("RAG 写入程序正在等待当前资料操作完成")
        else:
            job.stop()
        self._forget(name, job, stopped=True)
        self._cancel_reload(name)
        self.store.record(actor, "process.stop", name)

    def stop_all(self, actor):
        self._reload_pending.clear()
        errors = []
        for name in reversed(tuple(self._jobs)):
            try:
                self.stop(actor, name)
            except PROCESS_ERRORS as exc:
                errors.append(f"{name} {exc}")
        if errors:
            raise RuntimeError("；".join(errors))

    def reload(self, actor, *, only=None):
        running = {name for name in tuple(self._jobs) if self._job(name) is not None}
        apps = RELOAD_APPS | {process for name in self._publications for process in PUBLICATION_SERVICES[name]}
        planned = running | self._reload_pending
        selected = {only} if isinstance(only, str) else set(only or ())
        roots = planned & (selected if only is not None else apps | self._reload_pending)
        if not roots:
            return {}
        graph = {name: spec.depends_on for name, spec in self.services.items()}
        order = [
            name for name in TopologicalSorter(graph).static_order()
            if name in planned
            and (name in roots or any(self._depends_on(self.services[name], root) for root in roots))
        ]
        for name in order:
            self._command_and_env(self.services[name])
        self.store.record(actor, "process.reload", ",".join(sorted(selected)) if only is not None else "apps")

        errors, results, failed = [], {}, set()
        try:
            for name in reversed(order):
                if name in running:
                    self.stop(actor, name)
        except PROCESS_ERRORS as exc:
            errors.append(f"{name} 停止失败（{exc}）。已尝试恢复先前停止的进程。")
        finally:
            self._reload_pending.update(order)
            for name in order:
                try:
                    if self._job(name) is not None:
                        self._reload_pending.discard(name)
                        continue
                    if any(self._depends_on(self.services[name], dependency) for dependency in failed):
                        errors.append(f"{name} 等待依赖进程恢复")
                        continue
                    results[name] = self.start(actor, name)
                except PROCESS_ERRORS as exc:
                    failed.add(name)
                    errors.append(f"{name} 启动失败（{exc}）")
        if errors:
            raise RuntimeError("\n".join(errors))
        return results

    def close(self):
        with self._lock:
            for job in self._jobs.values():
                job.close()
            self._jobs.clear()
