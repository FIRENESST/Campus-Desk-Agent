"""Milvus storage and retrieval for the campus knowledge base."""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Protocol
from urllib.parse import urlsplit

import httpx2
from pymilvus import DataType, MilvusClient
from pymilvus.exceptions import MilvusException
from filelock import Timeout as StorageLockTimeout

if __package__:
    from .rag_embedding import (
        MODEL_DIMENSION, MODEL_NAME, MODEL_SPACE, project_embeddings, project_query_embedding, setup_model,
    )
    from .rag_lexical import LexicalIndex
    from .rag_navigation import navigate
    from .rag_ranking import rank_hits
    from .rag_storage import DEFAULT_COLLECTION, DEFAULT_URI, database_access_lock
else:
    from rag_embedding import (
        MODEL_DIMENSION, MODEL_NAME, MODEL_SPACE, project_embeddings, project_query_embedding, setup_model,
    )
    from rag_lexical import LexicalIndex
    from rag_navigation import navigate
    from rag_ranking import rank_hits
    from rag_storage import DEFAULT_COLLECTION, DEFAULT_URI, database_access_lock


PROJECT_SPACE = (MODEL_SPACE, MODEL_DIMENSION)
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
MAX_SOURCE_BYTES = 2_048
MAX_DIMENSION = 32_768
SCHEMA_VERSION = "rag_chunks_v1"
SCHEMA_PROPERTY = "rag.schema"
OUTPUT_FIELDS = ["document_id", "source", "chunk_index", "text"]


class RagError(ValueError):
    pass


class Embeddings(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class OpenAIEmbeddings:
    """Adapter for an OpenAI-compatible POST /embeddings endpoint."""

    def __init__(self, model: str, base_url: str, client: httpx2.Client):
        url = urlsplit(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.query
            or url.fragment
        ):
            raise RagError("向量服务地址必须是 HTTP(S) 基础地址")
        self.model = model
        self._url = base_url.rstrip("/") + "/embeddings"
        self._client = client

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for offset in range(0, len(texts), 16):
            batch = texts[offset : offset + 16]
            try:
                response = self._client.post(self._url, json={"model": self.model, "input": batch})
                response.raise_for_status()
                items = response.json()["data"]
                if not isinstance(items, list) or len(items) != len(batch):
                    raise ValueError("embedding count mismatch")
                by_index = {}
                for item in items:
                    index = item["index"]
                    if type(index) is not int or not 0 <= index < len(batch) or index in by_index:
                        raise ValueError("invalid embedding index")
                    by_index[index] = item["embedding"]
                vectors.extend(by_index[index] for index in range(len(batch)))
            except httpx2.HTTPStatusError as exc:
                raise RagError(f"向量服务返回 HTTP {exc.response.status_code}") from None
            except httpx2.RequestError:
                raise RagError("无法访问向量服务") from None
            except (KeyError, TypeError, ValueError):
                raise RagError("向量服务响应格式无效") from None
        return vectors


def _scope(scope: str) -> str:
    if not isinstance(scope, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", scope):
        raise RagError("资料范围需使用 1 到 64 个英文字母、数字、下划线或连字符")
    return scope


def _document_filter(scope: str, document_id: str) -> str:
    scope = _scope(scope)
    if not isinstance(document_id, str) or not re.fullmatch(r"[0-9a-f]{64}", document_id):
        raise RagError("文档 ID 无效")
    return f'scope == "{scope}" and document_id == "{document_id}"'


def _validate_search_options(scope: str, top_k: int) -> None:
    _scope(scope)
    if type(top_k) is not int or not 1 <= top_k <= 50:
        raise RagError("top_k 需为 1 到 50 的整数")


def validate_search(query: str, *, scope: str, top_k: int) -> str:
    _validate_search_options(scope, top_k)
    normalized = query.strip() if isinstance(query, str) else ""
    if not normalized or len(normalized) > 4_000:
        raise RagError("检索问题需包含 1 到 4000 个字符")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        raise RagError("检索问题包含无效 Unicode 字符") from None
    return normalized


def _embedding_space(
    embedder: Embeddings | None, existing: tuple[str, int] | None
) -> tuple[str, int]:
    if embedder is None:
        if existing is not None and existing != PROJECT_SPACE:
            raise RagError("集合的向量版本或维度与项目配置不兼容，请在新集合中重新导入原始资料")
        return PROJECT_SPACE

    # Keep the explicit adapter available for collections created before the model was fixed.
    if existing is None or existing[0] == MODEL_SPACE:
        raise RagError(f"项目集合固定使用 {MODEL_NAME}，请直接调用导入或检索接口")
    if embedder.model != existing[0]:
        raise RagError(f"旧集合需要其原有向量模型 {existing[0]}")
    return existing


def _chunks(text: str, fits: Callable[[str], bool] | None = None) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunk = text[start:end]
        while fits is not None and not fits(chunk):
            if end - start == 1:
                raise RagError("无法在模型词元上限内切分正文")
            end = start + (end - start) // 2
            chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        if end == len(text):
            break
        start = end - min(CHUNK_OVERLAP, (end - start) // 2)
    return chunks


def _vectors(rows: list[list[float]], expected_count: int, expected_dimension: int) -> list[list[float]]:
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RagError("向量数量与输入文本数量不一致")
    if not rows or not isinstance(rows[0], list):
        raise RagError("向量服务需要返回浮点数组")
    dimension = len(rows[0])
    if not 2 <= dimension <= MAX_DIMENSION or dimension != expected_dimension:
        raise RagError("向量维度与集合不兼容")

    normalized = []
    for row in rows:
        if not isinstance(row, list) or len(row) != dimension:
            raise RagError("向量维度不一致")
        try:
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in row):
                raise RagError("向量包含无效数值")
        except OverflowError:
            raise RagError("向量数值超出浮点范围") from None
        largest = max(abs(value) for value in row)
        if largest == 0:
            raise RagError("余弦检索需要非零向量")
        # Positive rescaling preserves cosine similarity and keeps float32 values in range.
        normalized.append([value / largest for value in row])
    return normalized


class RagDatabase:
    """One shared database handle per process for a local Milvus Lite directory."""

    def __init__(
        self, uri: str | Path = DEFAULT_URI, collection: str = DEFAULT_COLLECTION, *, token: str = ""
    ):
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,127}", collection):
            raise RagError("Milvus 集合名无效")
        address = str(uri)
        self._local = "://" not in address
        self._lexical = None
        if self._local:
            path = Path(address).resolve()
            if path.suffix != ".db":
                raise RagError("本地 Milvus 路径应以 .db 结尾")
            path.parent.mkdir(parents=True, exist_ok=True)
            address = str(path)
            self._lexical = LexicalIndex(path.with_name(f"{path.name}.{collection}.lexical.sqlite3"))
        self._retrieval_mode = os.getenv("RAG_RETRIEVAL_MODE", "adaptive").strip().lower()
        if self._retrieval_mode not in {"dense", "hybrid", "adaptive"}:
            raise RagError("RAG_RETRIEVAL_MODE 应为 dense、hybrid 或 adaptive")
        self.collection = collection
        self._metric = "COSINE"
        self._client = MilvusClient(uri=address, token=token, timeout=20)
        self._lock = Lock()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        with self._lock:
            self._client.close()

    def _metadata(self) -> tuple[str, int] | None:
        if not self._client.has_collection(self.collection):
            return None
        description = self._client.describe_collection(self.collection)
        try:
            stored = (
                description["properties"].get(SCHEMA_PROPERTY)
                if self._local
                else description["description"]
            )
            metadata = json.loads(stored)
            if metadata["format"] != SCHEMA_VERSION:
                raise ValueError("incompatible schema")
            model = metadata["embedding_model"]
            dimension = metadata["dimension"]
            metric = metadata.get("vector_metric", "COSINE")
            vector = next(field for field in description["fields"] if field["name"] == "vector")
            if (
                not isinstance(model, str)
                or not model
                or type(dimension) is not int
                or int(vector["params"]["dim"]) != dimension
                or metric not in {"IP", "COSINE"}
                or (metric == "IP" and (model, dimension) != PROJECT_SPACE)
            ):
                raise ValueError("incompatible embedding space")
            self._metric = metric
            return model, dimension
        except (KeyError, StopIteration, TypeError, ValueError):
            raise RagError("指定集合与 RAG 数据结构不匹配，请使用新的集合名") from None

    def _create_collection(self, model: str, dimension: int) -> None:
        # The fixed project encoder always produces unit vectors. IP has the same score
        # as cosine without re-normalizing every stored candidate during Lite search.
        metric = "IP" if self._local and (model, dimension) == PROJECT_SPACE else "COSINE"
        description = json.dumps(
            {"format": SCHEMA_VERSION, "embedding_model": model, "dimension": dimension, "vector_metric": metric},
            ensure_ascii=False,
        )
        schema = self._client.create_schema(
            auto_id=False, enable_dynamic_field=False, description=description
        )
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("document_id", DataType.VARCHAR, max_length=64)
        schema.add_field("scope", DataType.VARCHAR, max_length=64)
        schema.add_field("source", DataType.VARCHAR, max_length=MAX_SOURCE_BYTES)
        schema.add_field("chunk_index", DataType.INT64)
        schema.add_field("text", DataType.VARCHAR, max_length=CHUNK_SIZE * 4)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dimension)
        # Lite persists collection properties but omits the description from its schema adapter.
        properties = {SCHEMA_PROPERTY: description} if self._local else None
        self._client.create_collection(
            self.collection, schema=schema, properties=properties, consistency_level="Strong"
        )

    def _ensure_collection(self, space: tuple[str, int]) -> None:
        metadata = self._metadata()
        if metadata is None and space == PROJECT_SPACE:
            self._invalidate_lexical()
            self._create_collection(*space)
            metadata = self._metadata()
        if metadata != space:
            raise RagError("集合的向量版本或维度不兼容，请在新集合中重新导入原始资料")
        if not self._client.list_indexes(self.collection, field_name="vector"):
            index_params = self._client.prepare_index_params()
            index_params.add_index(field_name="vector", index_type="FLAT", metric_type=self._metric)
            self._client.create_index(self.collection, index_params=index_params)

    def initialize(self) -> dict:
        with self._lock:
            self._ensure_collection(PROJECT_SPACE)
        return self.status()

    def status(self) -> dict:
        with self._lock:
            metadata = self._metadata()
            if metadata is None:
                return {"collection": self.collection, "collection_created": False, "chunks": 0,
                        "embedding_model": None, "dimension": None}
            model, dimension = metadata
            stats = self._client.get_collection_stats(self.collection)
            return {"collection": self.collection, "collection_created": True,
                    "chunks": int(stats["row_count"]), "embedding_model": model, "dimension": dimension}

    def _iterate_rows(self, expression: str, fields: list[str]):
        self._client.load_collection(self.collection)
        iterator = self._client.query_iterator(
            self.collection, filter=expression, output_fields=fields,
            batch_size=256, consistency_level="Strong",
        )
        try:
            while batch := iterator.next():
                yield from batch
        finally:
            iterator.close()

    def _query_rows(self, expression: str, fields: list[str]) -> list[dict]:
        with self._lock:
            return list(self._iterate_rows(expression, fields)) if self._metadata() is not None else []

    def rebuild_lexical(self) -> dict:
        if self._lexical is None:
            raise RagError("词法索引仅用于本机 Milvus Lite 集合")
        with self._lock:
            if self._metadata() is None:
                raise RagError("知识库尚未初始化")
            rows = self._iterate_rows("chunk_index >= 0", ["chunk_id", "scope", *OUTPUT_FIELDS])
            try:
                count = self._lexical.rebuild(rows)
            finally:
                rows.close()
        return {"collection": self.collection, "chunks": count, "lexical_index": "ready"}

    def _invalidate_lexical(self) -> bool:
        try:
            return self._lexical is not None and self._lexical.invalidate()
        except (sqlite3.Error, OSError):
            raise RagError("无法更新词法索引状态，请先运行 reindex-lexical 再改动资料") from None

    def _sync_lexical(self, scope: str, document_id: str, rows: list[dict]) -> None:
        if self._lexical is not None:
            try:
                self._lexical.replace_document(scope, document_id, rows)
            except (sqlite3.Error, OSError):
                # The primary write has committed. The dirty marker keeps readers on dense search.
                print("词法索引暂不可用，请运行 reindex-lexical", file=sys.stderr)

    def list_documents(self, *, scope: str) -> list[dict]:
        scope = _scope(scope)
        # The first chunk identifies one revision without fetching its full text.
        rows = self._query_rows(f'scope == "{scope}" and chunk_index == 0', ["document_id", "source"])
        return sorted(
            ({"document_id": row["document_id"], "source": row["source"]} for row in rows),
            key=lambda row: (row["source"].casefold(), row["document_id"]),
        )

    def list_scopes(self) -> list[str]:
        rows = self._query_rows("chunk_index == 0", ["scope"])
        return sorted({row["scope"] for row in rows})

    def document_chunks(self, document_id: str, *, scope: str) -> list[dict]:
        rows = self._query_rows(_document_filter(scope, document_id), ["chunk_index", "text"])
        return sorted(rows, key=lambda row: row["chunk_index"])

    def delete_document(self, document_id: str, *, scope: str) -> None:
        expression = _document_filter(scope, document_id)
        with self._lock:
            if self._metadata() is None:
                return
            self._client.load_collection(self.collection)
            sync_lexical = self._invalidate_lexical()
            self._client.delete(self.collection, filter=expression)
            self._client.flush(self.collection)
            if sync_lexical:
                self._sync_lexical(scope, document_id, [])

    def ingest(
        self, text: str, *, source: str, scope: str, embedder: Embeddings | None = None
    ) -> dict:
        scope = _scope(scope)
        text = text.strip()
        source = source.strip()
        if not text:
            raise RagError("文档正文不能为空")
        if not source or len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise RagError("资料来源为空或超过长度限制")

        with self._lock:
            metadata = self._metadata()
        space = _embedding_space(embedder, metadata)
        if embedder is None:
            local_model = project_embeddings()
            parts = _chunks(text, local_model.fits)
            vectors = local_model.embed(parts)
        else:
            parts = _chunks(text)
            vectors = _vectors(embedder.embed(parts), len(parts), space[1])
        document_id = hashlib.sha256(
            json.dumps([scope, source, text], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        rows = [
            {
                "chunk_id": hashlib.sha256(f"{document_id}:{index}".encode()).hexdigest(),
                "document_id": document_id,
                "scope": scope,
                "source": source,
                "chunk_index": index,
                "text": part,
                "vector": vector,
            }
            for index, (part, vector) in enumerate(zip(parts, vectors, strict=True))
        ]

        with self._lock:
            self._ensure_collection(space)
            sync_lexical = self._invalidate_lexical()
            self._client.upsert(self.collection, data=rows)
            self._client.flush(self.collection)
            if sync_lexical:
                self._sync_lexical(scope, document_id, rows)
        return {"document_id": document_id, "chunks": len(rows)}

    def search(
        self, query: str, *, scope: str, embedder: Embeddings | None = None, top_k: int = 5
    ) -> list[dict]:
        query = validate_search(query, scope=scope, top_k=top_k)
        with self._lock:
            metadata = self._metadata()
        if metadata is None:
            return []
        _, dimension = _embedding_space(embedder, metadata)
        if embedder is None:
            vectors = [list(project_query_embedding(query))]
        else:
            vectors = _vectors(embedder.embed([query]), 1, dimension)
        return self._search_vectors(vectors, scope=scope, top_k=top_k, query=query)

    def _search_project_vector(
        self, vector: list[float], *, scope: str, top_k: int, query: str | None = None,
    ) -> list[dict]:
        """Search with a fixed-model query vector from the local RAG service."""
        if query is None:
            _validate_search_options(scope, top_k)
        else:
            query = validate_search(query, scope=scope, top_k=top_k)
        with self._lock:
            metadata = self._metadata()
        if metadata is None:
            return []
        _embedding_space(None, metadata)
        if (not isinstance(vector, (tuple, list)) or len(vector) != MODEL_DIMENSION
                or any(not isinstance(value, (float, int)) or not math.isfinite(value) for value in vector)
                or not any(vector)):
            raise RagError("查询向量与项目向量空间不兼容")
        return self._search_vectors([vector], scope=scope, top_k=top_k, query=query)

    def _search_vectors(
        self, vectors: list[list[float]], *, scope: str, top_k: int, query: str | None,
    ) -> list[dict]:
        # Scope is a validated ASCII identifier and cannot change the filter expression.
        limit = min(120, max(12, 5 * top_k)) if query is not None else top_k
        scope_filter = f'scope == "{scope}"'
        cached: list[dict] | None = None

        def dense():
            nonlocal cached
            if cached is None:
                data = vectors
                if self._metric == "IP":
                    data = []
                    for vector in vectors:
                        length = math.hypot(*vector)
                        if not math.isfinite(length) or not length:
                            raise RagError("查询向量与项目向量空间不兼容")
                        data.append([value / length for value in vector])
                matches = self._client.search(
                    collection_name=self.collection, data=data, anns_field="vector",
                    filter=scope_filter, limit=limit, output_fields=OUTPUT_FIELDS,
                    search_params={"metric_type": self._metric}, consistency_level="Strong",
                )
                cached = [{"chunk_id": hit["chunk_id"] if "chunk_id" in hit else hit["id"],
                           "score": max(-1.0, min(1.0, hit["distance"])), **hit["entity"]}
                          for hit in (matches[0] if matches else [])]
            return cached

        def hydrate(ids):
            # Even with a local side index, Milvus applies scope and supplies the authoritative text and vectors.
            try:
                rows = self._client.query(
                    self.collection, filter=f"{scope_filter} and chunk_id in {json.dumps(ids)}",
                    limit=len(ids), output_fields=["chunk_id", "vector", *OUTPUT_FIELDS], consistency_level="Strong",
                )
            except MilvusException:
                return []
            q = vectors[0]
            norm = math.hypot(*q)
            for row in rows:
                vec = row.pop("vector")
                length = norm * math.hypot(*vec)
                row["score"] = max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(q, vec)) / length)) if length else 0.0
            return rows

        with self._lock:
            self._client.load_collection(self.collection)
            if self._lexical is not None and query is not None and self._retrieval_mode != "dense":
                try:
                    with self._lexical.reader() as reader:
                        if reader is not None:
                            return navigate(
                                query, top_k, dense,
                                lambda part, phrase: reader.lookup(scope, part, phrase=phrase, limit=min(50, max(15, 2 * top_k))),
                                hydrate, mode=self._retrieval_mode,
                            )
                except (sqlite3.Error, OSError):
                    pass  # The Milvus vector index can serve traffic during an unavailable lexical index.
            hits = dense()
        return rank_hits(query, hits, top_k) if query is not None else hits


def main() -> int:
    parser = argparse.ArgumentParser(description="Milvus RAG 知识库")
    parser.add_argument("--uri", default=os.getenv("RAG_MILVUS_URI", str(DEFAULT_URI)))
    parser.add_argument("--collection", default=os.getenv("RAG_COLLECTION", DEFAULT_COLLECTION))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup-model", help="下载并校验项目的固定向量模型")
    commands.add_parser("init", help="初始化固定向量集合并查看状态")
    commands.add_parser("reindex-lexical", help="由 Milvus 原始切片构建或修复本机词法索引")
    start = commands.add_parser("start", help="初始化数据库并启动查询服务和写入窗口")
    start.add_argument("--port", type=int, help="固定查询端口，缺省时优先使用 8002")
    ingest = commands.add_parser("ingest", help="导入 UTF-8 文档")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--source", help="资料来源，缺省使用文件名")
    ingest.add_argument("--scope", default="campus")
    search = commands.add_parser("search", help="按语义相似度检索片段")
    search.add_argument("query")
    search.add_argument("--scope", default="campus")
    search.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    try:
        if args.command == "start":
            if __package__:
                from .rag_start import start_rag
            else:
                from rag_start import start_rag
            return start_rag(args.uri, args.collection, args.port)
        if args.command == "setup-model":
            setup_model()
            result = {"embedding_model": MODEL_SPACE, "dimension": MODEL_DIMENSION}
        else:
            with database_access_lock(args.uri), RagDatabase(args.uri, args.collection, token=os.getenv("RAG_MILVUS_TOKEN", "")) as db:
                if args.command == "init":
                    result = db.initialize()
                elif args.command == "reindex-lexical":
                    result = db.rebuild_lexical()
                elif args.command == "ingest":
                    text = args.file.read_text(encoding="utf-8-sig")
                    result = db.ingest(text, source=args.source or args.file.name, scope=args.scope)
                else:
                    result = db.search(args.query, scope=args.scope, top_k=args.top_k)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except MilvusException as exc:
        print(f"Milvus 操作失败（错误码 {exc.code}）", file=sys.stderr)
    except StorageLockTimeout:
        print("本地知识库正忙，请稍后重试", file=sys.stderr)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
