"""Operator-controlled configuration for the unified service entry points."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from dotenv import dotenv_values

from request_auth import LOCAL_AUTH_PIPE


MANAGER_DIR = Path(__file__).resolve().parent
SERVER_DIR = MANAGER_DIR.parent
MAIN_AGENT_DIR = SERVER_DIR / "MainAgent"
RAG_DIR = SERVER_DIR.parent / "DataBase" / "RAG"
DEFAULT_PORT = 8090
DEFAULT_PUBLIC_URL = f"http://127.0.0.1:{DEFAULT_PORT}"
DEFAULT_MINTA_URL = "http://127.0.0.1:19000"


def python_in(directory: Path) -> Path:
    if os.name == "nt":
        return directory / ".venv" / "Scripts" / "python.exe"
    return directory / ".venv" / "bin" / "python"


def _dotenv(path: Path) -> dict:
    return dotenv_values(path, encoding="utf-8-sig", interpolate=False) if path.is_file() else {}


def _validate_url(name: str, value: str, *, origin_only: bool = False) -> None:
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.query or parts.fragment
            or parts.port == 0 or any(char.isspace() or char in "\\*" for char in value)
            or (origin_only and parts.path not in {"", "/"})
        ):
            raise ValueError
        httpx2.URL(value)
    except (ValueError, httpx2.InvalidURL):
        raise ValueError(f"{name} 必须是无凭据和查询参数的 HTTP/HTTPS 地址") from None


@dataclass(frozen=True)
class Settings:
    public_url: str = DEFAULT_PUBLIC_URL
    main_url: str = "http://127.0.0.1:8081"
    rag_url: str = "http://127.0.0.1:8002"
    llm_url: str = "http://127.0.0.1:8000"
    mcp_url: str = "http://127.0.0.1:8001/mcp"
    minta_url: str = DEFAULT_MINTA_URL
    campus_digest_port: int = 8184
    campus_rumor_port: int = 8100
    admin_token: str = field(default="", repr=False)
    mcp_token: str = field(default="", repr=False)
    llm_token: str = field(default="", repr=False)
    llm_upstream_key: str = field(default="", repr=False)
    allow_remote_admin: bool = False
    rag_scope: str = "campus"
    rag_uri: str = ""
    rag_collection: str = ""
    rag_token: str = field(default="", repr=False)
    routing_data_dir: str = ""
    routing_embedding_port: int = 8002
    mpm_auth: tuple[str, int] | None = None
    web_admin_bridge: str = field(default="", repr=False)
    web_admin_state: Path | None = None
    runtime_status_key: str = field(default="", repr=False)
    campus_rumor_key: str = field(default="", repr=False)
    main_python: Path = field(default_factory=lambda: python_in(MAIN_AGENT_DIR))
    rag_python: Path = field(default_factory=lambda: python_in(RAG_DIR))

    def __post_init__(self):
        _validate_url("UIM_PUBLIC_URL", self.public_url, origin_only=True)
        for name in ("main_url", "rag_url", "llm_url", "mcp_url", "minta_url"):
            value = getattr(self, name)
            if value or name != "minta_url":
                _validate_url(f"UIM_{name.upper()}", value)
        gateway_tokens = []
        for name, value in (
            ("UIM_ADMIN_TOKEN", self.admin_token), ("MAIN_AGENT_MCP_TOKEN", self.mcp_token),
            ("UIM_LLM_TOKEN", self.llm_token),
        ):
            if value:
                if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value):
                    raise ValueError(f"{name} 需为 32 到 256 个 URL 安全字符")
                gateway_tokens.append(value)
        if len(set(gateway_tokens)) != len(gateway_tokens):
            raise ValueError("管理员、MCP 和模型调用凭据需分别设置")
        if any(not 33 <= ord(char) <= 126 for char in self.llm_upstream_key):
            raise ValueError("llmServer 本地密钥需使用不含空白的 ASCII 字符")
        if self.llm_upstream_key and self.llm_upstream_key in gateway_tokens:
            raise ValueError("统一入口的凭据需独立于 llmServer 本地密钥")
        if type(self.allow_remote_admin) is not bool:
            raise ValueError("UIM_ALLOW_REMOTE_ADMIN 必须是布尔值")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.rag_scope):
            raise ValueError("UIM_RAG_SCOPE 必须是有效的 RAG 资料范围")
        if self.routing_data_dir and not Path(self.routing_data_dir).is_absolute():
            raise ValueError("CLOUD_ROUTING_DATA_DIR 必须为本机绝对路径")
        if type(self.routing_embedding_port) is not int or not 0 <= self.routing_embedding_port <= 65535:
            raise ValueError("CLOUD_ROUTING_EMBEDDING_PORT 必须是 0 到 65535 的整数")
        if type(self.campus_digest_port) is not int or not 1 <= self.campus_digest_port <= 65535:
            raise ValueError("CAMPUS_DIGEST_PORT 必须是 1 到 65535 的整数")
        if type(self.campus_rumor_port) is not int or not 1 <= self.campus_rumor_port <= 65535:
            raise ValueError("CAMPUS_RUMOR_PORT 必须是 1 到 65535 的整数")
        if self.mpm_auth is not None:
            address, pid = self.mpm_auth
            if not LOCAL_AUTH_PIPE.fullmatch(address) or type(pid) is not int or not 1 <= pid <= 0xffffffff:
                raise ValueError("主管理器鉴权需要有效的本机命名管道及进程 ID")
        if self.web_admin_bridge or self.web_admin_state is not None:
            if (not self.mpm_auth or not re.fullmatch(r"[A-Za-z0-9_-]{43}", self.web_admin_bridge)
                or self.web_admin_state is None or not self.web_admin_state.is_absolute()):
                raise ValueError("在线管理入口需要主管理器的鉴权通道与本机状态目录")
        if self.runtime_status_key and not re.fullmatch(r"[A-Za-z0-9_-]{43}", self.runtime_status_key):
            raise ValueError("运行状态凭据格式无效")
        if self.campus_rumor_key and not re.fullmatch(r"[A-Za-z0-9_-]{43}", self.campus_rumor_key):
            raise ValueError("校园传闻服务凭据格式无效")

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        local = _dotenv(env_file if env_file is not None else MANAGER_DIR / ".env")
        model_service = _dotenv(SERVER_DIR / "llmServer" / ".env")
        mcp_service = _dotenv(SERVER_DIR / "mcpServer" / ".env")
        main_service = _dotenv(MAIN_AGENT_DIR / ".env.cloud")

        def get(name, default=""):
            return os.environ.get(name, local.get(name, default) or "").strip()

        remote = get("UIM_ALLOW_REMOTE_ADMIN", "false").lower()
        auth_pipe, auth_pid = get("MPM_AUTH_PIPE"), get("MPM_AUTH_PID")
        mpm_auth = None
        if auth_pipe or auth_pid:
            if not auth_pipe or not re.fullmatch(r"[1-9][0-9]{0,9}", auth_pid):
                raise ValueError("主管理器鉴权需要同时提供 MPM_AUTH_PIPE 和 MPM_AUTH_PID")
            mpm_auth = auth_pipe, int(auth_pid)
        if remote not in {"true", "false"}:
            raise ValueError("UIM_ALLOW_REMOTE_ADMIN 必须是 true 或 false")
        return cls(
            public_url=get("UIM_PUBLIC_URL", DEFAULT_PUBLIC_URL),
            main_url=get("UIM_MAIN_URL", "http://127.0.0.1:8081"),
            rag_url=get("UIM_RAG_URL", "http://127.0.0.1:8002"),
            llm_url=get("UIM_LLM_URL") or f"http://127.0.0.1:{get('LOCAL_PORT', model_service.get('LOCAL_PORT') or '8000')}",
            mcp_url=get("UIM_MCP_URL", "http://127.0.0.1:8001/mcp"),
            minta_url=get("UIM_MINTA_URL", DEFAULT_MINTA_URL),
            campus_digest_port=int(get("CAMPUS_DIGEST_PORT") or "8184"),
            campus_rumor_port=int(get("CAMPUS_RUMOR_PORT") or "8100"),
            admin_token=get("UIM_ADMIN_TOKEN"),
            mcp_token=get("MAIN_AGENT_MCP_TOKEN", mcp_service.get("MAIN_AGENT_MCP_TOKEN")),
            llm_token=get("UIM_LLM_TOKEN"),
            llm_upstream_key=get("LOCAL_API_KEY", model_service.get("LOCAL_API_KEY")),
            allow_remote_admin=remote == "true",
            rag_scope=get("UIM_RAG_SCOPE", "campus"),
            rag_uri=get("RAG_MILVUS_URI"),
            rag_collection=get("RAG_COLLECTION"),
            rag_token=get("RAG_MILVUS_TOKEN"),
            routing_data_dir=get("CLOUD_ROUTING_DATA_DIR", main_service.get("CLOUD_ROUTING_DATA_DIR")),
            routing_embedding_port=int(get("CLOUD_ROUTING_EMBEDDING_PORT", main_service.get("CLOUD_ROUTING_EMBEDDING_PORT") or "8002")),
            mpm_auth=mpm_auth,
            web_admin_bridge=os.getenv("MPM_WEB_ADMIN_BRIDGE", ""),
            web_admin_state=Path(admin_state) if (admin_state := os.getenv("MPM_WEB_ADMIN_STATE")) else None,
            runtime_status_key=os.getenv("MPM_RUNTIME_STATUS_KEY", ""),
            campus_rumor_key=os.getenv("MPM_RUMOR_BRIDGE_KEY", ""),
            main_python=Path(get("UIM_MAIN_PYTHON", str(python_in(MAIN_AGENT_DIR)))),
            rag_python=Path(get("UIM_RAG_PYTHON", str(python_in(RAG_DIR)))),
        )
