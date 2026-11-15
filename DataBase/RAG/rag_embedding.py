"""Fixed local text embeddings for the campus knowledge base."""

import hashlib
import os
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx2
import numpy as np


MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_DIMENSION = 512
MODEL_CONTEXT = 512
MODEL_REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
MODEL_SPACE = f"{MODEL_NAME}@qdrant-{MODEL_REVISION[:12]}-cls-v1"
MODEL_DIR = Path(__file__).resolve().parent / "models" / "bge-small-zh-v1.5"
MODEL_FILE = "model_optimized.onnx"
TOKENIZER_FILE = "tokenizer.json"
BATCH_SIZE = 8
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
ARTIFACTS = {
    TOKENIZER_FILE: "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    MODEL_FILE: "1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38",
}
DOWNLOAD_HOSTS = ("https://huggingface.co", "https://hf-mirror.com")


class EmbeddingError(ValueError):
    pass


class QueryTooLongError(EmbeddingError):
    pass


def _matches(path: Path, digest: str) -> bool:
    try:
        with path.open("rb") as artifact:
            return hashlib.file_digest(artifact, "sha256").hexdigest() == digest
    except FileNotFoundError:
        return False


def _download(client: httpx2.Client, filename: str, digest: str, path: Path) -> None:
    for host in DOWNLOAD_HOSTS:
        url = f"{host}/Qdrant/bge-small-zh-v1.5/resolve/{MODEL_REVISION}/{filename}?download=true"
        with NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as artifact:
            temporary = Path(artifact.name)
        try:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with temporary.open("wb") as artifact:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        artifact.write(chunk)
            if _matches(temporary, digest):
                temporary.replace(path)
                return
        except httpx2.HTTPError:
            continue
        finally:
            temporary.unlink(missing_ok=True)
    raise EmbeddingError(f"无法下载或校验本地向量模型文件 {filename}，请重试 setup-model")


def setup_model() -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    timeout = httpx2.Timeout(45, connect=10)
    with httpx2.Client(timeout=timeout, follow_redirects=True) as client:
        for filename, digest in ARTIFACTS.items():
            path = MODEL_DIR / filename
            if not _matches(path, digest):
                _download(client, filename, digest, path)


class ProjectEmbeddings:
    def __init__(self) -> None:
        for filename, digest in ARTIFACTS.items():
            if not _matches(MODEL_DIR / filename, digest):
                raise EmbeddingError("本地向量模型缺失或校验失败，请运行 python -m rag_db setup-model")

        try:
            import onnxruntime
            from tokenizers import Tokenizer
        except (ImportError, OSError) as exc:
            raise EmbeddingError("无法加载向量模型依赖，请安装 requirements.txt") from exc

        try:
            self._tokenizer = Tokenizer.from_file(str(MODEL_DIR / TOKENIZER_FILE))
            self._tokenizer.no_truncation()
            self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = min(os.cpu_count() or 1, 4)
            self._session = onnxruntime.InferenceSession(
                str(MODEL_DIR / MODEL_FILE), sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise EmbeddingError("无法加载本地向量模型") from exc

    def fits(self, text: str) -> bool:
        return len(self._tokenizer.encode(text).ids) <= MODEL_CONTEXT

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for offset in range(0, len(texts), BATCH_SIZE):
            batch = self._tokenizer.encode_batch(texts[offset : offset + BATCH_SIZE])
            if len(batch[0].ids) > MODEL_CONTEXT:
                raise EmbeddingError(f"向量输入超过 {MODEL_CONTEXT} 个词元")
            inputs = {
                "input_ids": np.asarray([item.ids for item in batch], dtype=np.int64),
                "attention_mask": np.asarray([item.attention_mask for item in batch], dtype=np.int64),
                "token_type_ids": np.asarray([item.type_ids for item in batch], dtype=np.int64),
            }
            try:
                tokens = self._session.run(["last_hidden_state"], inputs)[0]
            except Exception as exc:
                raise EmbeddingError("本地向量推理失败") from exc
            rows = tokens[:, 0, :]  # BGE uses the CLS token for the dense embedding.
            norms = np.linalg.norm(rows, axis=1, keepdims=True)
            if (
                rows.shape != (len(batch), MODEL_DIMENSION)
                or not np.isfinite(norms).all()
                or (norms == 0).any()
            ):
                raise EmbeddingError("本地模型返回的向量无效")
            vectors.extend((rows / norms).tolist())
        return vectors

    def embed_query(self, query: str) -> list[float]:
        text = QUERY_PREFIX + query
        if not self.fits(text):
            raise QueryTooLongError(f"检索问题超过模型的 {MODEL_CONTEXT} 词元上限，请缩短后重试")
        return self.embed([text])[0]


@lru_cache(maxsize=1)
def project_embeddings() -> ProjectEmbeddings:
    return ProjectEmbeddings()


@lru_cache(maxsize=128)
def project_query_embedding(query: str) -> tuple[float, ...]:
    return tuple(project_embeddings().embed_query(query))
