"""Read-only HTTP retrieval and embeddings for local callers."""

import argparse
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Thread

from milvus_lite.exceptions import DataDirLockedError
from pymilvus.exceptions import MilvusException
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

if __package__:
    from .rag_db import DEFAULT_COLLECTION, DEFAULT_URI, RagDatabase, RagError, validate_search
    from .rag_embedding import MODEL_SPACE, EmbeddingError, QueryTooLongError, project_embeddings, project_query_embedding
    from .rag_worker import RagWorkerError, run_worker
else:
    from rag_db import DEFAULT_COLLECTION, DEFAULT_URI, RagDatabase, RagError, validate_search
    from rag_embedding import MODEL_SPACE, EmbeddingError, QueryTooLongError, project_embeddings, project_query_embedding
    from rag_worker import RagWorkerError, run_worker


API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8002
MAX_REQUEST_BYTES = 64 * 1024
MAX_ROUTING_TEXTS = 32
MAX_ROUTING_TEXT_CHARS = 400


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _database_query(payload: dict, field: str) -> JSONResponse:
    try:
        result = await run_in_threadpool(
            run_worker, Path(__file__), payload, timeout=30, lock_timeout=5,
        )
    except RagWorkerError as exc:
        return _error(str(exc), 503)
    if "error" in result:
        return _error(result["error"], 503)
    if field not in result:
        return _error("知识库工作进程返回了无效结果", 503)
    return JSONResponse(result)


async def _request_json(request: Request) -> object:
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        return _error("请求需要 application/json", 415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            return _error("请求体超过 64 KiB", 413)
        body.extend(chunk)
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        return _error("请求体需要有效的 JSON", 400)


async def _search(request: Request) -> JSONResponse:
    params = await _request_json(request)
    if isinstance(params, JSONResponse):
        return params
    if not isinstance(params, dict) or params.keys() - {"query", "scope", "top_k"}:
        return _error("请求体需为包含 query、scope 或 top_k 的 JSON 对象", 422)
    scope = params.get("scope", "campus")
    top_k = params.get("top_k", 5)
    try:
        query = validate_search(params.get("query"), scope=scope, top_k=top_k)
        vector = await run_in_threadpool(project_query_embedding, query)
    except (RagError, QueryTooLongError) as exc:
        return _error(str(exc), 422)
    except EmbeddingError as exc:
        return _error(str(exc), 503)
    return await _database_query({"vector": vector, "query": query, "scope": scope, "top_k": top_k}, "hits")


def _embed_routing_texts(texts: list[str]) -> list[list[float] | None]:
    encoder = project_embeddings()
    usable = [(index, text) for index, text in enumerate(texts) if encoder.fits(text)]
    vectors = encoder.embed([text for _, text in usable])
    result: list[list[float] | None] = [None] * len(texts)
    for (index, _), vector in zip(usable, vectors, strict=True):
        result[index] = vector
    return result


async def _routing_embeddings(request: Request) -> JSONResponse:
    if request.client is None or request.client.host != API_HOST:
        return _error("路由向量接口仅接受本机请求", 403)
    params = await _request_json(request)
    if isinstance(params, JSONResponse):
        return params
    texts = params.get("texts") if isinstance(params, dict) and params.keys() == {"texts"} else None
    if (
        not isinstance(texts, list) or not 1 <= len(texts) <= MAX_ROUTING_TEXTS
        or any(not isinstance(text, str) or not text.strip() or len(text) > MAX_ROUTING_TEXT_CHARS for text in texts)
    ):
        return _error("texts 需要包含 1 到 32 条非空文本，每条最多 400 字符", 422)
    try:
        vectors = await run_in_threadpool(_embed_routing_texts, texts)
    except (EmbeddingError, UnicodeError):
        return _error("本机路由向量暂不可用", 503)
    return JSONResponse({"space": MODEL_SPACE, "vectors": vectors})


async def _scopes(request: Request) -> JSONResponse:
    return await _database_query({"operation": "scopes"}, "scopes")


def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ready"})


@asynccontextmanager
async def _lifespan(app: Starlette):
    await run_in_threadpool(project_embeddings)
    yield


app = Starlette(
    routes=[
        Route("/search", _search, methods=["POST"]),
        Route("/routing/embeddings", _routing_embeddings, methods=["POST"]),
        Route("/scopes", _scopes),
        Route("/health", _health),
    ],
    middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=[API_HOST, "localhost"])],
    lifespan=_lifespan,
)


def _worker_main() -> None:
    request = json.load(sys.stdin)
    try:
        with RagDatabase(
            os.getenv("RAG_MILVUS_URI", str(DEFAULT_URI)),
            os.getenv("RAG_COLLECTION", DEFAULT_COLLECTION),
            token=os.getenv("RAG_MILVUS_TOKEN", ""),
        ) as database:
            if request.get("operation") == "scopes":
                result = {"scopes": database.list_scopes()}
            else:
                result = {"hits": database._search_project_vector(
                    request["vector"], scope=request["scope"], top_k=request["top_k"], query=request.get("query"),
                )}
    except RagError as exc:
        result = {"error": str(exc)}
    except DataDirLockedError:
        result = {"error": "本地知识库正忙，请稍后重试"}
    except (MilvusException, OSError):
        result = {"error": "暂时无法访问 Milvus 知识库，请检查库状态"}
    print(json.dumps(result))


def _serve_managed(port: int | None) -> None:
    import uvicorn

    # The service itself retains the socket, so another process cannot claim
    # the selected port between a probe and the actual startup.
    with socket.socket() as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((API_HOST, DEFAULT_API_PORT if port is None else port))
        except OSError:
            if port is not None:
                raise
            listener.bind((API_HOST, 0))

        actual_port = listener.getsockname()[1]
        config = uvicorn.Config(
            app, host=API_HOST, port=actual_port, access_log=False, timeout_graceful_shutdown=5,
        )
        server = uvicorn.Server(config)

        def wait_for_shutdown() -> None:
            os.read(sys.stdin.fileno(), 1)
            server.should_exit = True

        Thread(target=wait_for_shutdown, daemon=True).start()
        print(actual_port, flush=True)
        server.run(sockets=[listener])


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="本机 RAG 检索 API")
    parser.add_argument("--port", type=int, help=f"本机端口，默认 {DEFAULT_API_PORT}")
    parser.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.managed:
        try:
            _serve_managed(args.port)
        except OSError:
            port = args.port if args.port is not None else DEFAULT_API_PORT
            parser.exit(1, f"查询服务无法绑定本机端口 {port}\n")
    else:
        port = DEFAULT_API_PORT if args.port is None else args.port
        uvicorn.run(app, host=API_HOST, port=port, access_log=False)


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        _worker_main()
    else:
        main()
