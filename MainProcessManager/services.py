"""Project service definitions and the format for additional registrations."""

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .control import local_path


SERVICE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")
CORE = ("postgresql", "llmserver", "rag-query", "mcpserver", "teacher-credentials", "cas-server", "campus-digest", "campus-rumor",
        "main-agent", "unified-interface", "android-app-download")
RELOAD_APPS = frozenset(("mcpserver", "main-agent", "main-worker", "teacher-credentials", "cas-server",
                         "campus-digest", "campus-rumor", "unified-interface", "android-app-download", "user-website"))
SIMULATIONS = ("sim-gymnasium", "sim-club", "sim-library", "sim-classroom", "sim-schedule")


@dataclass(frozen=True)
class Service:
    name: str
    cwd: Path
    argv: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    listener: bool = False
    postgres_data: Path | None = None
    window: bool = False
    env: tuple[tuple[str, str], ...] = ()
    start_all: bool = True

    def command(self):
        file = Path(self.argv[0])
        if file.is_absolute() or file.parent != Path(".") or (self.cwd / file).is_file():
            executable = self.cwd / file
        else:
            match = shutil.which(self.argv[0])
            if not match:
                raise ValueError(f"找不到 {self.name} 的可执行程序")
            executable = Path(match)
        executable = local_path(executable)
        if not self.cwd.is_dir() or executable.suffix.lower() != ".exe" or not executable.is_file():
            raise ValueError(f"{self.name} 缺少工作目录或可执行程序，请检查登记")
        return [str(executable), *self.argv[1:]]


def _python(name, directory, *args, depends_on=(), listener=True, window=False):
    interpreter = directory / ".venv" / "Scripts" / "python.exe"
    return Service(name, directory, (str(interpreter), "-B", *args), depends_on, listener, window=window)


def project_services(root: Path) -> dict[str, Service]:
    server = root / "server"
    data = root / "DataBase"
    postgres = data / "PostgreSQL"
    simulation = root / "simulation"
    agent = server / "MainAgent"
    cas_python = server / "CASserver" / ".venv" / "Scripts" / "python.exe"
    if not cas_python.is_file():
        cas_python = agent / ".venv" / "Scripts" / "python.exe"
    rag = data / "RAG"
    digest_dir = root / (os.getenv("CAMPUS_DIGEST_DIR") or "integrations/campus-digest")
    digest_python = digest_dir / "runtime" / "python" / "python.exe"
    rumor_dir = root / (os.getenv("CAMPUS_RUMOR_DIR") or "integrations/campus-rumor")
    rumor_runtime = server / "CampusRumor" / ".runtime" / "python" / "python.exe"
    rumor_python = root / (os.getenv("CAMPUS_RUMOR_PYTHON") or (
        rumor_runtime if rumor_runtime.is_file() else rumor_dir / ".venv" / "Scripts" / "python.exe"
    ))
    postgres_bin = root / (os.getenv("MPM_POSTGRES_BIN") or postgres / "pgsql" / "bin")
    minta = root / (os.getenv("MINTAGW_EXE") or root / "MlNTA" / "core" / "target" / "debug" / "mintagw.exe")
    app_dependencies = ("postgresql", "llmserver")
    services = (
        Service("postgresql", postgres, (str(postgres_bin / "pg_ctl.exe"),),
                listener=True, postgres_data=postgres / "data"),
        _python("llmserver", server / "llmServer", "server.py"),
        _python("rag-query", rag, "rag_api.py"),
        _python("rag-writer", rag, "rag_writer.py", depends_on=("rag-query",), listener=False, window=True),
        _python("mcpserver", server / "mcpServer", "main.py", depends_on=("rag-query",)),
        _python("teacher-credentials", agent, "-m", "main_agent.teacher_credentials", depends_on=("postgresql",)),
        Service("cas-server", root, (str(cas_python), "-B", "-m", "server.CASserver"),
                listener=True),
        Service("campus-digest", root, (str(digest_python), "-B", "-X", "utf8", "-m", "server.CampusDigest.managed",
                                         str(digest_dir)), listener=True, start_all=digest_python.is_file()),
        Service("campus-rumor", root, (str(rumor_python), "-B", "-X", "utf8", str(server / "CampusRumor" / "managed.py"),
                                       str(rumor_dir)), listener=True,
                start_all=(rumor_dir / "app" / "main.py").is_file() and rumor_python.is_file()),
        _python("main-agent", agent, "-m", "main_agent.cloud", "serve", depends_on=app_dependencies),
        _python("main-worker", agent, "-m", "main_agent.cloud", "worker", depends_on=app_dependencies, listener=False),
        _python("unified-interface", server / "UnifiedInterfaceManagement", "main.py", "serve"),
        Service("android-app-download", server / "AndroidAppServer", ("node.exe", "server.mjs"), listener=True),
        Service("user-website", server / "UserWebsite",
                ("node.exe", "--env-file-if-exists=.env", "server.mjs"),
                ("main-agent", "unified-interface", "cas-server", "android-app-download"), listener=True),
        _python("sim-gymnasium", simulation / "Gymnasium", "-m", "uvicorn", "app:app",
                "--host", "127.0.0.1", "--port", "19001"),
        Service("sim-club", simulation / "Club", ("node.exe", "server.js"), listener=True),
        Service("sim-library", simulation / "Library",
                ("java.exe", "-cp", "out/classes", "library.LibraryServer"), listener=True),
        Service("sim-classroom", simulation / "Classroom", ("bin/Debug/net10.0/Classroom.exe",), listener=True),
        Service("sim-schedule", simulation / "Schedule", ("target/debug/schedule.exe",), listener=True),
        Service("mintagw", simulation,
                (str(minta), "run", "--config", "mintagw.toml"), SIMULATIONS, listener=True,
                start_all=minta.is_file()),
    )
    return {service.name: service for service in services}


def custom_service(name: str, data: dict, root: Path) -> Service:
    if not SERVICE_NAME.fullmatch(name) or name in {"all", "core"}:
        raise ValueError("进程名须为小写字母开头，最多 40 个小写字母、数字、下划线或连字符")
    if not isinstance(data, dict) or data.keys() - {"cwd", "argv", "depends_on", "listener"}:
        raise ValueError("进程定义使用 cwd、argv、depends_on 和 listener")
    directory, argv = data.get("cwd"), data.get("argv")
    deps, listener = data.get("depends_on", []), data.get("listener", False)
    if (
        not isinstance(directory, str) or not directory or "\0" in directory
        or not isinstance(argv, list) or not 1 <= len(argv) <= 64 or not argv[0]
        or any(not isinstance(part, str) or "\0" in part or len(part) > 4_096 for part in argv)
        or not isinstance(deps, list) or len(deps) > 32
        or any(not isinstance(dep, str) or not SERVICE_NAME.fullmatch(dep) for dep in deps)
        or len(set(deps)) != len(deps) or name in deps or type(listener) is not bool
    ):
        raise ValueError("进程定义字段或依赖无效")
    return Service(name, local_path(root / directory), tuple(argv), tuple(deps), listener)


def read_definition(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(65_537)
    if len(raw) > 65_536:
        raise ValueError("进程定义文件不能超过 64 KiB")
    return json.loads(raw.decode("utf-8-sig"))
