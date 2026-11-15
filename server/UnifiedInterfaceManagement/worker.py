"""Bridge into the RAG and MainAgent virtual environments over private stdio."""

import base64
import json
import os
import sqlite3
import sys
from contextlib import redirect_stdout
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_DIR = Path(__file__).resolve().parents[2]


class WorkerError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def read_material(arguments):
    if "text" in arguments:
        return arguments["source"], arguments["text"]

    from rag_materials import MAX_FILE_BYTES, SUPPORTED_SUFFIXES, load_material

    name = arguments["filename"]
    if not name or any(char in "/\\:" or ord(char) < 32 for char in name):
        raise ValueError("请提供单个文件名")
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError("此文件格式暂不支持导入 RAG")
    try:
        data = base64.b64decode(arguments["data_base64"], validate=True)
    except ValueError:
        raise ValueError("文件 Base64 无效") from None
    if not 0 < len(data) <= MAX_FILE_BYTES:
        raise WorkerError(413, "invalid_file_size", "RAG 文件应为非空且不超过 20 MiB")
    with TemporaryDirectory(prefix="uim-rag-") as temporary:
        path = Path(temporary) / f"document{suffix}"
        path.write_bytes(data)
        return arguments.get("source", name), load_material(path)


def rag(action, arguments):
    sys.path.insert(0, str(PROJECT_DIR / "DataBase" / "RAG"))
    from milvus_lite.exceptions import DataDirLockedError
    from pymilvus.exceptions import MilvusException

    from rag_db import DEFAULT_COLLECTION, DEFAULT_URI, RagDatabase
    from rag_embedding import EmbeddingError

    scope = arguments["scope"]
    uri = os.getenv("RAG_MILVUS_URI", str(DEFAULT_URI))
    try:
        if action == "ingest":
            source, text = read_material(arguments)
        with RagDatabase(
            uri, os.getenv("RAG_COLLECTION", DEFAULT_COLLECTION),
            token=os.getenv("RAG_MILVUS_TOKEN", ""),
        ) as database:
            match action:
                case "list":
                    return {"scope": scope, "documents": database.list_documents(scope=scope)}
                case "view":
                    document_id = arguments["document_id"]
                    chunks = database.document_chunks(document_id, scope=scope)
                    if not chunks:
                        raise WorkerError(404, "document_not_found", "资料版本不存在")
                    return {"scope": scope, "document_id": document_id, "chunks": chunks}
                case "ingest":
                    return {"scope": scope, "source": source, **database.ingest(text, source=source, scope=scope)}
                case "delete":
                    document_id = arguments["document_id"]
                    database.delete_document(document_id, scope=scope)
                    return {"scope": scope, "document_id": document_id, "deleted": True}
                case _:
                    raise WorkerError(404, "unknown_operation", "未知的资料管理操作")
    except DataDirLockedError:
        raise WorkerError(503, "rag_busy", "RAG 库正忙，请稍后重试") from None
    except (MilvusException, EmbeddingError, OSError):
        raise WorkerError(503, "rag_unavailable", "RAG 库或向量模型暂不可用") from None


def routing(action, arguments):
    sys.path.insert(0, str(PROJECT_DIR / "server" / "MainAgent"))
    from main_agent.campus_cluster import AgentId, RoutingFeedback, build_campus_cluster
    from main_agent.campus_cluster.routing_similarity import embed_locally

    port = int(os.getenv("CLOUD_ROUTING_EMBEDDING_PORT", "8002")) if action == "preview" else 0
    store = RoutingFeedback(os.environ["CLOUD_ROUTING_DATA_DIR"], embed=partial(embed_locally, port) if port else None)
    match action:
        case "status":
            return {"enabled": store.is_enabled()}
        case "set_enabled":
            store.set_enabled(arguments["enabled"])
            return {"enabled": arguments["enabled"]}
        case "overview":
            data = store.overview(arguments["days"])
            overrides = data.pop("introductions")
            data["roles"] = [
                {"id": profile.id.value, "name": profile.name,
                 "introduction": overrides.get(profile.id, profile.responsibility),
                 "overridden": profile.id in overrides}
                for profile in build_campus_cluster().registry.profiles
            ]
            return data
        case "introductions":
            overrides = store.introductions()
            return {profile.id.value: overrides.get(profile.id, profile.responsibility)
                    for profile in build_campus_cluster().registry.profiles}
        case "set_introduction":
            role = AgentId(arguments["role"])
            store.set_introduction(role, arguments["text"])
            return {"updated": role.value}
        case "reset_introduction":
            role = AgentId(arguments["role"])
            return {"reset": store.reset_introduction(role)}
        case "preview":
            return store.preview(arguments["prompt"])
        case "list":
            return store.samples_page(**arguments)
        case "grade":
            sample_id = arguments["sample_id"]
            targets = tuple(AgentId(value) for value in arguments["targets"])
            if not store.grade(sample_id, arguments["score"], targets, direct=arguments.get("direct", False)):
                raise WorkerError(404, "sample_not_found", "路由样本不存在")
            return {"graded": sample_id}
        case "clear_grade":
            sample_id = arguments["sample_id"]
            if not store.clear_grade(sample_id):
                raise WorkerError(404, "sample_not_found", "路由样本不存在")
            return {"cleared": sample_id}
        case "bulk_delete":
            return {"deleted_count": store.delete_samples(tuple(arguments["sample_ids"]))}
        case "delete":
            sample_id = arguments["sample_id"]
            if not store.delete_sample(sample_id):
                raise WorkerError(404, "sample_not_found", "路由样本不存在")
            return {"deleted": sample_id}
        case _:
            raise WorkerError(404, "unknown_operation", "未知的路由管理操作")


def postgres(action, arguments):
    sys.path.insert(0, str(PROJECT_DIR / "server" / "MainAgent"))
    from main_agent.cloud.admin_data import operator_operation
    from main_agent.cloud.errors import ServiceError

    try:
        if action == "campus_profiles_bulk_review":
            sys.path.append(str(PROJECT_DIR))
            from server.CASserver.store import CASStore

            accounts = CASStore(Path(arguments["cas_directory"])).identity_accounts
            return operator_operation(action, arguments, cas_accounts=accounts)
        return operator_operation(action, arguments)
    except ServiceError as exc:
        raise WorkerError(exc.status, exc.code, exc.message) from None


def main():
    operations = {"rag": rag, "routing": routing, "postgres": postgres}
    try:
        backend = operations[sys.argv[1]]
        request = json.load(sys.stdin)
        with redirect_stdout(sys.stderr):
            data = backend(request["action"], request["arguments"])
        result = {"status": 200, "data": data}
    except WorkerError as exc:
        result = {"status": exc.status, "data": {"error": {"code": exc.code, "message": str(exc)}}}
    except ValueError as exc:
        result = {"status": 422, "data": {"error": {"code": "invalid_argument", "message": str(exc)}}}
    except (OSError, ImportError, sqlite3.Error):
        result = {"status": 503, "data": {"error": {"code": "service_unavailable", "message": "本地服务未就绪"}}}
    def encode(value):
        from datetime import datetime
        from uuid import UUID
        if isinstance(value, (datetime, UUID)):
            return str(value)
        raise TypeError("unsupported worker output")

    print(json.dumps(result, ensure_ascii=False, allow_nan=False, default=encode))


if __name__ == "__main__":
    main()
