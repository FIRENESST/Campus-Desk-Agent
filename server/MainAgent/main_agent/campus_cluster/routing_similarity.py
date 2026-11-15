"""Semantic routing hints from locally encoded, reviewed examples."""

import logging
import math
from collections.abc import Callable
from functools import lru_cache

import httpx2


EmbeddingBatch = tuple[str, list[list[float] | None]]
EmbeddingProvider = Callable[[list[str]], EmbeddingBatch]
Vector = tuple[float, ...]

MAX_PROTOTYPES = 32
MAX_PROTOTYPES_PER_LABEL = 8
MAX_TEXT_CHARS = 400
MIN_SIMILARITY = 0.74
MIN_LABEL_GAP = 0.06


def embed_locally(port: int, texts: list[str]) -> EmbeddingBatch:
    with httpx2.Client(timeout=httpx2.Timeout(2, connect=0.3), trust_env=False, follow_redirects=False) as client:
        response = client.post(f"http://127.0.0.1:{port}/routing/embeddings", json={"texts": texts})
        response.raise_for_status()
        result = response.json()
    if not isinstance(result, dict) or not {"space", "vectors"} <= result.keys():
        raise ValueError("本机路由向量响应无效")
    return result["space"], result["vectors"]


def _embedding_text(text: str) -> str:
    text = "".join(char for char in " ".join(text.split()) if char.isprintable())
    return text if len(text) <= MAX_TEXT_CHARS and sum(char.isalnum() for char in text) >= 6 else ""


def _normalize_vector(row: list[float] | None) -> Vector | None:
    if row is None:
        return None
    if not isinstance(row, list) or not 1 <= len(row) <= 2048:
        raise ValueError("本机路由向量维度无效")
    if not all(type(value) in (int, float) and math.isfinite(value) for value in row):
        raise ValueError("本机路由向量包含无效值")
    norm = math.hypot(*row)
    if not norm or not math.isfinite(norm):
        raise ValueError("本机路由向量模长无效")
    return tuple(value / norm for value in row)


class SemanticRouting:
    def __init__(self, encode: EmbeddingProvider):
        self.encode = encode
        self._cached_prototypes = lru_cache(maxsize=1)(self._encode_prototypes)

    def _vectors(self, texts: list[str]) -> tuple[str, tuple[Vector | None, ...]]:
        space, rows = self.encode(texts)
        if not isinstance(space, str) or not space or not isinstance(rows, list) or len(rows) != len(texts):
            raise ValueError("本机路由向量批次无效")
        return space, tuple(_normalize_vector(row) for row in rows)

    def _encode_prototypes(self, space: str, texts: tuple[str, ...]) -> dict[str, Vector | None]:
        prototype_space, rows = self._vectors(list(texts))
        if prototype_space != space:
            raise ValueError("路由样例与当前请求使用了不同的向量版本")
        return dict(zip(texts, rows, strict=True))

    @staticmethod
    def _prototypes(examples: list[tuple[str, str]]) -> dict[tuple[str, str], None]:
        prototypes = {}
        per_label: dict[str, int] = {}
        for text, label in examples:
            text = _embedding_text(text)
            count = per_label.get(label, 0)
            if text and count < MAX_PROTOTYPES_PER_LABEL and (text, label) not in prototypes:
                prototypes[text, label] = None
                per_label[label] = count + 1
                if len(prototypes) >= MAX_PROTOTYPES:
                    break
        return prototypes

    def suggest(self, prompt: str, examples: list[tuple[str, str]]) -> str | None:
        prompt = _embedding_text(prompt)
        if not prompt or not examples:
            return None
        prototypes = self._prototypes(examples)
        if not prototypes:
            return None
        scores: dict[str, float] = {}
        try:
            space, (query,) = self._vectors([prompt])
            if query is None:
                return None
            texts = tuple(dict.fromkeys(text for text, _ in prototypes))
            vectors = self._cached_prototypes(space, texts)
            for text, label in prototypes:
                vector = vectors[text]
                if vector is not None:
                    score = sum(a * b for a, b in zip(query, vector, strict=True))
                    scores[label] = max(score, scores.get(label, -1.0))
        except (httpx2.HTTPError, OSError, ValueError, OverflowError, TypeError) as exc:
            logging.getLogger(__name__).debug("Local routing embeddings unavailable (%s)", type(exc).__name__)
            return None
        if not scores:
            return None
        best, *others = sorted(scores, key=scores.__getitem__, reverse=True)
        if scores[best] < MIN_SIMILARITY or (others and scores[best] - scores[others[0]] < MIN_LABEL_GAP):
            return None
        return best
