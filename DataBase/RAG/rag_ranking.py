"""Rank one pool of vector hits using lexical overlap and result diversity."""

import math
import re
import unicodedata
from collections import Counter


_HAN = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af"
_TERM_RUN = re.compile(rf"(?P<han>[{_HAN}]+)|[^\W_{_HAN}]+")
_WINDOW_CHARS = 200
_WINDOW_STEP = 100
_SOURCE_WEIGHT = 0.08
_REDUNDANCY_WEIGHT = 0.25
_REDUNDANCY_THRESHOLD = 0.55


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _terms(text: str) -> set[str]:
    terms = set()
    for match in _TERM_RUN.finditer(text):
        value = match[0]
        if match.lastgroup == "han" and len(value) > 1:
            for width in (2, 3):
                terms.update(value[start:start + width] for start in range(len(value) - width + 1))
        else:
            terms.add(value)
    return terms


def _best_passage_match(
    text: str, term_weights: dict[str, float], phrases: dict[str, bool], phrase_length: int,
) -> float:
    best = 0.0
    for start in range(0, len(text), _WINDOW_STEP):
        passage = text[start:start + _WINDOW_CHARS]
        terms = _terms(passage)
        coverage = math.fsum(term_weights.get(term, 0.0) for term in terms)
        exact = [phrase for phrase, han in phrases.items() if phrase in (passage if han else terms)]
        exact_coverage = sum(map(len, exact)) / phrase_length
        # A full specific phrase or several terms together are stronger than scattered n-grams.
        if len(exact) >= 2 or any(len(phrase) >= 5 for phrase in exact):
            match = 0.33 * coverage + 0.23 * exact_coverage
        else:
            match = 0.01 * coverage
        best = max(best, match)
    return best


def rank_hits(query: str, hits: list[dict], top_k: int) -> list[dict]:
    """Keep the original citation fields and cosine scores in the selected order."""
    if not hits:
        return []
    query = _normalize(query)
    query_terms = _terms(query)
    phrases = {
        match[0]: match.lastgroup == "han" for match in _TERM_RUN.finditer(query)
        if 3 <= len(match[0]) <= _WINDOW_CHARS
    }
    phrase_length = sum(map(len, phrases)) or 1
    texts = [_normalize(hit["text"]) for hit in hits]
    bodies = [_terms(text) for text in texts]
    sources = [_terms(_normalize(hit["source"])) for hit in hits]
    frequency = Counter(term for body, source in zip(bodies, sources) for term in query_terms & (body | source))
    weights = {term: 1 + math.log((len(hits) + 1) / (count + 1)) for term, count in frequency.items()}
    weight_total = math.fsum(weights.values()) or 1
    weights = {term: value / weight_total for term, value in weights.items()}
    relevance = []
    for hit, text, source in zip(hits, texts, sources):
        passage_match = _best_passage_match(text, weights, phrases, phrase_length)
        source_match = math.fsum(weights[term] for term in query_terms & source)
        relevance.append(hit["score"] + passage_match + _SOURCE_WEIGHT * source_match)

    remaining = list(range(len(hits)))
    redundancy = [0.0] * len(hits)
    selected = []
    while remaining and len(selected) < top_k:
        best = max(remaining, key=lambda index: (relevance[index] - redundancy[index], -index))
        selected.append(hits[best])
        remaining.remove(best)
        for index in remaining:
            common = len(bodies[best] & bodies[index])
            union = len(bodies[best]) + len(bodies[index]) - common
            overlap = common / union if union else 0.0
            penalty = _REDUNDANCY_WEIGHT * max(0.0, overlap - _REDUNDANCY_THRESHOLD)
            redundancy[index] = max(redundancy[index], penalty)
    return selected
