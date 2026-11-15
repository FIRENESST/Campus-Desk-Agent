"""Navigate the local search index inside one RAG request."""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from random import Random

if __package__:
    from .rag_lexical import lexical_tokens, reference_mentions
    from .rag_ranking import _normalize, rank_hits
else:
    from rag_lexical import lexical_tokens, reference_mentions
    from rag_ranking import _normalize, rank_hits


_SPLIT = re.compile(r"[。?!？！;；\n]+|以及|另外|顺便|并且|同时(?:请)?|和(?=如何|怎么|是否|能否|可否)")
_PREFIX = re.compile(r"^(?:(?:请(?:问|帮我|告诉我|查一下)?|我想(?:问|查|了解)?|帮我(?:查找|查一下|找)?|想了解|查一下)\s*)+")
_FOLLOWUP = re.compile(r"^(?:请帮我|官方)?(?:查找?|给我|找|提供)?(?:这[些个项]|相关的?|对应的?)?(?:校方|官方)?(?:规定|说明|通知|文件|出处|原文|链接)$")
_EXPLICIT = re.compile(r"《([^《》\n]{2,64})》|「([^「」\n]{2,64})」|“([^“”\n]{2,64})”|"
                       r"(?<![a-zA-Z0-9])([a-zA-Z]{1,8}[-_]?\d{2,}[a-zA-Z0-9_-]*)(?![a-zA-Z0-9])")
_FILLER = re.compile(r"能不能|可不可以|需要准备哪些|需要带什么|要准备什么|我想知道|想知道|有哪些|什么时候|怎么办|是哪个|是什么|多久之内|可以|是否|能否|需要|怎样|怎么|如何|多久|哪些")
_MAX_STEPS = 4
_MAX_DEPTH = 3
_ENOUGH = 0.72
_Hit = dict


def _keywords(text: str) -> frozenset[str]:
    return frozenset(term for term in lexical_tokens(text) if len(term) > 1)


@dataclass(frozen=True)
class QueryPlan:
    focus: str
    facets: tuple[str, ...]
    anchors: tuple[str, ...]


def plan_query(query: str) -> QueryPlan:
    quotes = (next(x for x in match.groups() if x) for match in _EXPLICIT.finditer(_normalize(query)))
    anchors = tuple(dict.fromkeys([*(mention[0] for mention in reference_mentions(query)), *quotes]))[:2]
    cleaned = (_PREFIX.sub("", _normalize(part)).strip(" ，,") for part in _SPLIT.split(query))
    facets = tuple(dict.fromkeys(
        " ".join(_FILLER.sub(" ", part).split()) for part in cleaned if len(part) >= 2 and not _FOLLOWUP.fullmatch(part)
    ))[:3]
    facets = tuple(part for part in facets if _keywords(part))
    if not facets:
        facets = (_normalize(query),)
    return QueryPlan(" ".join(facets), facets, anchors)


def _hard_anchor(anchor: str, text: str) -> bool:
    if references := reference_mentions(anchor):
        return any(code == target and (not issuer or observed.endswith(issuer) or (observed and issuer.endswith(observed)))
                   for _, target, issuer in references for _, code, observed in reference_mentions(text))
    if anchor.isascii() and any(char.isdigit() for char in anchor):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(anchor)}(?![a-z0-9])", text))
    if any(char.isdigit() for char in anchor):
        tokens = lexical_tokens(anchor)
        return bool(tokens) and f" {' '.join(tokens)} " in f" {' '.join(lexical_tokens(text))} "
    return anchor in text


class _Evidence:
    def __init__(self, plan: QueryPlan):
        self.facets = tuple(_keywords(text) for text in plan.facets)
        self.anchors = plan.anchors
        self._scores: dict[str, tuple[tuple[float, ...], int]] = {}

    def scores(self, hit: _Hit) -> tuple[tuple[float, ...], int]:
        key = hit["chunk_id"]
        if key in self._scores:
            return self._scores[key]
        body = _normalize(hit["text"])
        title = _normalize(hit["source"])
        # Coverage must be local to a passage; unrelated words spread over one chunk do not count together.
        passages = [set(lexical_tokens(body[i:i + 200])) for i in range(0, len(body), 120)]
        passages.append(set(lexical_tokens(title)))
        coverage = tuple(max((len(terms & passage) for passage in passages), default=0) / len(terms) if terms else 0
                         for terms in self.facets)
        hard = sum(1 << i for i, anchor in enumerate(self.anchors)
                   if _hard_anchor(anchor, body) or _hard_anchor(anchor, title))
        result = coverage, hard
        self._scores[key] = result
        return result

    def coverage(self, hits: list[_Hit]) -> tuple[tuple[float, ...], int]:
        coverage = (0.0,) * len(self.facets)
        anchors = 0
        for hit in hits:
            scores, hard = self.scores(hit)
            coverage = tuple(map(max, coverage, scores))
            anchors |= hard
        return coverage, anchors

    def enough(self, hits: list[_Hit]) -> bool:
        coverage, hard = self.coverage(hits)
        return all(score >= _ENOUGH for score in coverage) and hard.bit_count() == len(self.anchors)


@dataclass(frozen=True)
class _Action:
    text: str
    phrase: bool = False
    facet: int | None = None

    @property
    def key(self) -> tuple[bool, tuple[str, ...]]:
        tokens = lexical_tokens(self.text)
        return (self.phrase and len(tokens) > 1, tuple(tokens if self.phrase else sorted(set(tokens))))


@dataclass
class _Node:
    hits: list[_Hit]
    depth: int = 0
    children: list["_Node"] = field(default_factory=list)
    visits: int = 0
    total: float = 0.0

    def uct(self, parent_visits: int) -> float:
        return self.total / self.visits + 0.24 * math.sqrt(math.log1p(parent_visits) / self.visits)


def _unique(hits: list[_Hit]) -> list[_Hit]:
    return list({hit["chunk_id"]: hit for hit in hits}.values())


def _alternate_actions(plan: QueryPlan, evidence: _Evidence, node: _Node) -> list[_Action]:
    coverage, hard = evidence.coverage(node.hits)
    actions = [_Action(anchor, True) for i, anchor in enumerate(plan.anchors) if not (hard & (1 << i))]
    for i, text in sorted(enumerate(plan.facets), key=lambda item: coverage[item[0]]):
        if coverage[i] < _ENOUGH:
            actions.append(_Action(text, facet=i))
            # Try a strict anchor or a compact group when the full facet admits broad matches.
            keywords = sorted(set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]+", text)), key=lambda x: (-len(x), x))
            for group in keywords[:2]:
                actions.append(_Action(group, phrase=len(lexical_tokens(group)) > 1, facet=i))
    return actions


def _rollout(node: _Node, evidence: _Evidence, random: Random, top_k: int) -> float:
    # Sample small possible answer contexts from the retrieved evidence. Branches with
    # multiple useful passages get a steadier reward than one accidental token match.
    hits = sorted(node.hits, key=lambda item: (evidence.scores(item)[1].bit_count(), max(evidence.scores(item)[0])),
                  reverse=True)[:2 * max(top_k, 4)]
    reward = 0.0
    for _ in range(5):
        sample = random.sample(hits, min(max(2, top_k), len(hits)))
        scores, hard = evidence.coverage(sample)
        reward += sum(scores) / len(scores) + 0.25 * hard.bit_count() / max(1, len(evidence.anchors))
    return reward / (5 * (1 + 0.04 * node.depth))


def _expand(root: _Node, plan: QueryPlan, evidence: _Evidence, find: Callable[[_Action], list[_Hit]],
            used: set[tuple[bool, tuple[str, ...]]], steps: int, top_k: int) -> dict[_Action, list[_Hit]]:
    """Bounded tree search: select with UCT, expand a query, score coverage, back up reward."""
    pools: dict[_Action, list[_Hit]] = {}
    best = root.hits
    random = Random(plan.focus)
    for _ in range(steps):
        if evidence.enough(best):
            break
        path = [root]
        while True:
            node = path[-1]
            actions = [a for a in _alternate_actions(plan, evidence, node) if a.key not in used]
            children = [child for child in node.children if child.depth < _MAX_DEPTH
                        and any(a.key not in used for a in _alternate_actions(plan, evidence, child))]
            if node.depth < _MAX_DEPTH and actions and (len(node.children) < 2 or not children):
                action = actions[0]
                used.add(action.key)
                hits = find(action)
                if hits:
                    pools[action] = hits
                best = _unique([*best, *hits])
                child = _Node(_unique([*node.hits, *hits]), depth=node.depth + 1)
                node.children.append(child)
                path.append(child)
                reward = _rollout(child, evidence, random, top_k)
                for branch in path:
                    branch.visits += 1
                    branch.total += reward
                break
            if not children:
                return pools
            path.append(max(children, key=lambda child: child.uct(node.visits)))
    return pools


def _fuse(query: str, plan: QueryPlan, evidence: _Evidence, dense: list[_Hit], lexical: dict[_Action, list[_Hit]],
          top_k: int, *, adaptive: bool) -> list[_Hit]:
    if not lexical:
        return rank_hits(query, dense, top_k)
    sem = {hit["chunk_id"]: rank for rank, hit in enumerate(rank_hits(query, dense, len(dense)), 1)}
    lex: dict[str, int] = {}
    facet_ranks = [{} for _ in plan.facets]
    for action, pool in lexical.items():
        for rank, hit in enumerate(pool, 1):
            key = hit["chunk_id"]
            lex[key] = min(lex.get(key, rank), rank)
            if action.facet is not None:
                facet = facet_ranks[action.facet]
                facet[key] = min(facet.get(key, rank), rank)
    weights = (0.52, 0.48)
    if adaptive:
        lexical_coverage, hard = evidence.coverage([hit for pool in lexical.values() for hit in pool[:6]])
        vector_coverage, _ = evidence.coverage(dense[:6])
        delta = sum(left - right for left, right in zip(lexical_coverage, vector_coverage)) / len(plan.facets)
        lexical_weight = 0.65 if hard else min(0.55, max(0.28, 0.4 + 0.24 * delta))
        weights = (1 - lexical_weight, lexical_weight)
    all_hits = _unique([*(hit for pool in lexical.values() for hit in pool), *dense])
    scores = {}
    for hit in all_hits:
        key = hit["chunk_id"]
        cov, hard = evidence.scores(hit)
        rrf = sum(weight * 12 / (11 + rank[key]) for weight, rank in zip(weights, (sem, lex)) if key in rank)
        scores[key] = rrf + 0.29 * max(cov) + (0.32 * bool(hard) if adaptive else 0)

    result: list[_Hit] = []
    seen: set[str] = set()
    covered = [0.0] * len(plan.facets)
    body = {hit["chunk_id"]: _normalize(hit["text"]) for hit in all_hits}
    while all_hits and len(result) < top_k:
        def priority(hit):
            key = hit["chunk_id"]
            cov, _ = evidence.scores(hit)
            coverage_gain = 0.65 * sum(max(0, value - prior) for value, prior in zip(cov, covered))
            if adaptive and len(plan.facets) > 1:
                coverage_gain += max((
                    (1 - prior) * 0.98 * 8 / (7 + ranks[key])
                    for value, prior, ranks in zip(cov, covered, facet_ranks, strict=True)
                    if key in ranks and value >= 0.30 and value > prior + 0.04
                ), default=0)
            repetition = (0.32 if adaptive else 0.18) if body[key] in seen else 0
            return scores[key] + (coverage_gain if adaptive else 0) - repetition

        best = max(all_hits, key=priority)
        result.append(best)
        seen.add(body[best["chunk_id"]])
        all_hits.remove(best)
        covered = list(map(max, covered, evidence.scores(best)[0]))
    return result


def navigate(query: str, top_k: int, dense: Callable[[], list[_Hit]],
             lexical: Callable[[str, bool], list[str]], hydrate: Callable[[list[str]], list[_Hit]],
             *, mode: str = "adaptive", max_steps: int = _MAX_STEPS) -> list[_Hit]:
    plan = plan_query(query)
    evidence = _Evidence(plan)
    known: dict[str, _Hit] = {}
    checked: set[str] = set()

    def find(action: _Action) -> list[_Hit]:
        ids = lexical(action.text, action.phrase)
        missing = [key for key in ids if key not in known and key not in checked]
        if missing:
            known.update((hit["chunk_id"], hit) for hit in hydrate(missing))
            checked.update(missing)
        return [known[key] for key in ids if key in known]

    if mode == "dense":
        return rank_hits(query, dense(), top_k)
    adaptive = mode == "adaptive"
    used: set[tuple[bool, tuple[str, ...]]] = set()
    pools: dict[_Action, list[_Hit]] = {}
    if adaptive and plan.anchors:
        initial = _Action(plan.anchors[0], True)
        hits = find(initial)
        used.add(initial.key)
        if hits:
            pools[initial] = hits
            if len(hits) >= top_k and evidence.enough(hits[:top_k]):
                return _fuse(query, plan, evidence, [], pools, top_k, adaptive=True)
    original = dense()
    known.update((hit["chunk_id"], hit) for hit in original)
    single_topic = len(plan.facets) == 1 and not plan.anchors
    initial = _Action(plan.focus if adaptive and not single_topic else query)
    if len(used) < max_steps:
        hits = find(initial)
        used.add(initial.key)
        if hits:
            pools[initial] = hits
    baseline = None
    if adaptive and single_topic:
        baseline = _fuse(query, plan, evidence, original, pools, top_k, adaptive=False)
        if evidence.enough(baseline):
            return baseline
    if adaptive:
        leading = _unique([*(hit for pool in pools.values() for hit in pool[:top_k]), *original[:top_k]])
        remaining = max_steps - len(used)
        extra = _expand(_Node(baseline or leading), plan, evidence, find, used, remaining, top_k)
        if baseline is not None and not extra:
            return baseline
        pools.update(extra)
    final = _fuse(query, plan, evidence, original, pools, top_k, adaptive=adaptive)
    if baseline is not None and evidence.coverage(final)[0] <= evidence.coverage(baseline)[0]:
        return baseline
    return final
