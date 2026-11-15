"""Validated administration calls into the existing service runtimes."""

import json
import os
import re
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import anyio
from filelock import Timeout
from pydantic import BaseModel, ConfigDict, Field, model_validator

from protocol import InterfaceError, json_input, validate
from settings import MAIN_AGENT_DIR, RAG_DIR, Settings

sys.path.append(str(RAG_DIR.parents[1]))
from DataBase.RAG.rag_storage import DEFAULT_COLLECTION, DEFAULT_URI, database_access_lock


class Input(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


ScopeName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class RagScope(Input):
    scope: ScopeName


class RagSearch(Input):
    query: Annotated[str, Field(min_length=1, max_length=4_000)]
    scope: ScopeName | None = None
    top_k: Annotated[int, Field(ge=1, le=50)] = 5


class RagIngest(Input):
    scope: ScopeName | None = None
    source: Annotated[str, Field(min_length=1, max_length=2_048)] | None = None
    text: Annotated[str, Field(min_length=1)] | None = None
    filename: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    data_base64: Annotated[str, Field(min_length=1, max_length=4 * ((20 * 1024 * 1024 + 2) // 3))] | None = None

    @model_validator(mode="after")
    def check_input(self):
        if self.text is not None:
            if self.source is None or self.filename is not None or self.data_base64 is not None:
                raise ValueError("文本导入需要来源和正文")
        elif self.filename is None or self.data_base64 is None or any(
            char in "/\\:" or ord(char) < 32 for char in self.filename
        ):
            raise ValueError("文件导入需要文件名和 Base64")
        return self


class Enabled(Input):
    enabled: bool


class Introduction(Input):
    text: Annotated[str, Field(min_length=1, max_length=1_200)]


class Grade(Input):
    score: Annotated[int, Field(ge=1, le=5)]
    targets: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(max_length=4)
    direct: bool = False

    @model_validator(mode="after")
    def check_target(self):
        if self.direct and self.targets:
            raise ValueError("直接回答不能同时指定子智能体")
        return self


SAMPLE_ID_PATTERN = r"^[0-9a-f]{32}$"


class RoutingSamples(Input):
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    offset: Annotated[int, Field(ge=0, le=2_147_483_647)] = 0
    status: Literal["all", "pending", "reviewed", "different"] = "all"
    agent: Annotated[str, Field(pattern=r"^[a-z_]{0,40}$")] = ""
    origin: Literal["", "automatic", "preferred"] = ""
    q: Annotated[str, Field(max_length=160)] = ""
    day: Annotated[str, Field(pattern=r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2})?$")] = ""


class RoutingOverview(Input):
    days: Annotated[int, Field(ge=1, le=90)] = 14


class RoutingPreview(Input):
    prompt: Annotated[str, Field(min_length=1, max_length=800)]


class RoutingBulkDelete(Input):
    sample_ids: list[Annotated[str, Field(pattern=SAMPLE_ID_PATTERN)]] = Field(min_length=1, max_length=100)


def _query(request, allowed: set[str]) -> dict:
    items = request.query_params.multi_items()
    fields = dict(items)
    if len(items) != len(fields) or fields.keys() - allowed:
        raise InterfaceError(422, "invalid_query", "查询参数无效或重复")
    return fields


def _routing_query(request, model, *integers):
    fields = _query(request, set(model.model_fields))
    for name in integers:
        if name in fields:
            if not re.fullmatch(r"[0-9]{1,10}", fields[name]):
                raise InterfaceError(422, "invalid_query", "路由查询参数无效")
            fields[name] = int(fields[name])
    return validate(model, fields).model_dump()


def worker_arguments(request, interface, raw: bytes, default_scope: str) -> dict:
    action = interface.target
    if interface.service == "rag_worker":
        if action == "ingest":
            _query(request, set())
            data = json_input(request, raw, RagIngest).model_dump(exclude_none=True)
            data.setdefault("scope", default_scope)
            return data
        fields = _query(request, {"scope"})
        fields.setdefault("scope", default_scope)
        scope = validate(RagScope, fields).scope
        if action == "list":
            return {"scope": scope}
        document_id = request.path_params["document_id"]
        if not re.fullmatch(r"[0-9a-f]{64}", document_id):
            raise InterfaceError(422, "invalid_document_id", "资料文档 ID 无效")
        return {"scope": scope, "document_id": document_id}

    if action == "overview":
        return _routing_query(request, RoutingOverview, "days")
    if action == "list":
        return _routing_query(request, RoutingSamples, "limit", "offset")
    _query(request, set())
    match action:
        case "set_enabled":
            return json_input(request, raw, Enabled).model_dump()
        case "set_introduction":
            return {"role": request.path_params["role"], **json_input(request, raw, Introduction).model_dump()}
        case "reset_introduction":
            return {"role": request.path_params["role"]}
        case "preview":
            return json_input(request, raw, RoutingPreview).model_dump()
        case "bulk_delete":
            return json_input(request, raw, RoutingBulkDelete).model_dump()
        case "grade" | "clear_grade" | "delete":
            sample_id = request.path_params["sample_id"]
            if not re.fullmatch(SAMPLE_ID_PATTERN, sample_id):
                raise InterfaceError(422, "invalid_sample_id", "路由样本 ID 无效")
            payload = json_input(request, raw, Grade).model_dump() if action == "grade" else {}
            return {"sample_id": sample_id, **payload}
        case _:
            return {}


class LocalWorkers:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._capacity = anyio.CapacityLimiter(2)

    async def call(self, service: str, action: str, arguments: dict) -> tuple[int, dict | list]:
        return await anyio.to_thread.run_sync(self._run, service, action, arguments, limiter=self._capacity)

    def _run(self, service: str, action: str, arguments: dict) -> tuple[int, dict | list]:
        settings = self.settings
        env = os.environ.copy()
        for name in ("MPM_AUTH_PIPE", "MPM_AUTH_PID", "MPM_WEB_ADMIN_BRIDGE", "MPM_WEB_ADMIN_STATE", "MPM_RUNTIME_STATUS_KEY"):
            env.pop(name, None)
        lock = nullcontext()
        if service == "rag_worker":
            interpreter, directory, backend = settings.rag_python, RAG_DIR, "rag"
            if not settings.rag_uri and urlsplit(settings.rag_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise InterfaceError(503, "rag_storage_unconfigured", "远程 RAG 需要明确配置 RAG_MILVUS_URI")
            uri = settings.rag_uri or str(DEFAULT_URI)
            if "://" not in uri:
                uri = str((directory / uri).resolve())
            env["RAG_MILVUS_URI"] = uri
            env["RAG_COLLECTION"] = settings.rag_collection or DEFAULT_COLLECTION
            env["RAG_MILVUS_TOKEN"] = settings.rag_token
            # Lite may keep its own lock through interpreter shutdown.
            lock = database_access_lock(uri, timeout=10)
        elif service == "routing_worker":
            interpreter, directory, backend = settings.main_python, MAIN_AGENT_DIR, "routing"
            if not settings.routing_data_dir:
                raise InterfaceError(503, "routing_unconfigured", "请配置 CLOUD_ROUTING_DATA_DIR")
            env["CLOUD_ROUTING_DATA_DIR"] = settings.routing_data_dir
            env["CLOUD_ROUTING_EMBEDDING_PORT"] = str(settings.routing_embedding_port)
        else:
            interpreter, directory, backend = settings.main_python, MAIN_AGENT_DIR, "postgres"
        interpreter = Path(interpreter).resolve()
        if not interpreter.is_file():
            raise InterfaceError(503, "worker_unconfigured", "服务的本地 Python 运行环境未就绪")
        try:
            with lock:
                result = subprocess.run(
                    [str(interpreter), "-B", "-X", "utf8", str(Path(__file__).with_name("worker.py")), backend],
                    input=json.dumps({"action": action, "arguments": arguments}, ensure_ascii=False).encode("utf-8"),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=directory, env=env,
                    timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except Timeout:
            raise InterfaceError(503, "rag_busy", "RAG 库正忙，请稍后重试") from None
        except subprocess.TimeoutExpired:
            raise InterfaceError(504, "worker_timeout", "本地管理操作超时，请查询当前状态") from None
        except OSError:
            raise InterfaceError(503, "worker_unavailable", "无法启动本地管理进程") from None
        try:
            if result.returncode or len(result.stdout) > 16 * 1024 * 1024:
                raise ValueError
            output = json.loads(result.stdout)
            status, data = output["status"], output["data"]
            if type(status) is not int or not 200 <= status <= 599 or not isinstance(data, (dict, list)):
                raise ValueError
            return status, data
        except (ValueError, TypeError, KeyError):
            raise InterfaceError(502, "worker_error", "本地管理进程未返回有效结果") from None
