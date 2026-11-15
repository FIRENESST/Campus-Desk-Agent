"""Compare local RAG enrichment portfolios on a pre-labeled read-only export."""

import argparse
from collections import defaultdict
from itertools import combinations
import json
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from time import perf_counter
import unicodedata

import numpy as np


HAN = re.compile(r"[\u3400-\u9fff]+|[0-9A-Za-z]+")
HEADER = re.compile(
    r"^第[〇一二三四五六七八九十百零\d]+(?:章|节|部分)\s*(\S.{1,36})$"
    r"|^([^\d，,。；;：:]{5,58}(?:办法|规定|细则|条例|章程|方案|守则|意见)(?:\(.{1,12}\))?)$"
)
ARTICLE = re.compile(r"^(?:第[〇一二三四五六七八九十百零\d]+条|[一二三四五六七八九十]+、)\s*")
REFERENCE = re.compile(
    r"[\[(〔【［]\s*((?:19|20)\d{2})\s*[\])〕】］]\s*(?:第\s*)?(\d{1,6})\s*号"
)
SENTENCE = re.compile(r"[。！？；\n]+")
VIEW_WEIGHTS = {"heading": 0.32, "lead": 0.28, "reference": 1.6}
DEPTH = 40


def normalize(value):
    return unicodedata.normalize("NFKC", value).casefold()


def terms(value):
    for match in HAN.finditer(normalize(value)):
        run = match[0]
        if "\u3400" <= run[0] <= "\u9fff" and len(run) > 1:
            for start in range(len(run) - 1):
                yield run[start:start + 2]
        else:
            yield run


def reference_codes(value):
    return [f"ref{year}x{int(number)}" for year, number in REFERENCE.findall(normalize(value))]


def heading_view(rows):
    by_document = defaultdict(list)
    for ordinal, row in enumerate(rows, 1):
        by_document[row["document_id"]].append((ordinal, row))
    projected = ["" for _ in rows]
    for document in by_document.values():
        recent = []
        for ordinal, row in sorted(document, key=lambda pair: pair[1]["chunk_index"]):
            chunk = row["chunk_index"]
            observed = [
                line.strip() for line in normalize(row["text"]).splitlines()
                if HEADER.fullmatch(line.strip())
            ]
            recent = [(index, line) for index, line in recent if index >= chunk - 3]
            recent.extend((chunk, line) for line in observed)
            projected[ordinal - 1] = " ".join(dict.fromkeys(line for _, line in recent[-3:]))
    return projected


def lead_view(rows):
    result = []
    for row in rows:
        candidates = []
        for sentence in SENTENCE.split(normalize(row["text"])):
            article = ARTICLE.match(sentence.strip())
            value = ARTICLE.sub("", sentence.strip())
            if len(value) >= 14 and not HEADER.fullmatch(value):
                candidates.append((article is None, value[:78]))
        candidates.sort(key=lambda pair: pair[0])
        result.append(" ".join(value for _, value in candidates[:2]))
    return result


def materialize(directory, name, rows):
    started = perf_counter()
    if name == "heading":
        values = heading_view(rows)
    elif name == "lead":
        values = lead_view(rows)
    elif name == "reference":
        values = [" ".join(dict.fromkeys(reference_codes(row["text"]))) for row in rows]
    else:
        values = [row["text"] for row in rows]
    path = directory / f"{name}.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE VIRTUAL TABLE passage USING fts5(terms, content='', tokenize='unicode61')")
        connection.executemany(
            "INSERT INTO passage(rowid, terms) VALUES(?, ?)",
            ((index, " ".join(terms(value))) for index, value in enumerate(values, 1) if value),
        )
        connection.commit()
    except Exception:
        connection.close()
        raise
    return connection, {
        "milliseconds": round(1000 * (perf_counter() - started), 1),
        "bytes": path.stat().st_size,
        "rows": sum(bool(value) for value in values),
    }


def retrieve(connection, query, view):
    candidates = reference_codes(query) if view == "reference" else terms(query)
    needle = " OR ".join(f'"{term}"' for term in dict.fromkeys(candidates))
    if not needle:
        return []
    hits = connection.execute(
        "SELECT rowid FROM passage WHERE passage MATCH ? ORDER BY bm25(passage), rowid LIMIT ?",
        (needle, DEPTH),
    )
    return [hit[0] for hit in hits]


def dense_ranks(rows, questions):
    if __package__:
        from .rag_embedding import ARTIFACTS, MODEL_DIR, QUERY_PREFIX, project_embeddings
    else:
        from rag_embedding import ARTIFACTS, MODEL_DIR, QUERY_PREFIX, project_embeddings
    if any(not (MODEL_DIR / name).is_file() for name in ARTIFACTS):
        raise ValueError("The fixed local embedding model must already be installed")
    encoder = project_embeddings()
    matrix = np.asarray([row["vector"] for row in rows], dtype=np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    queries = np.asarray(encoder.embed([QUERY_PREFIX + entry["query"] for entry in questions]), dtype=np.float32)
    similarities = queries @ matrix.T
    return [[int(index) + 1 for index in np.argsort(-scores)[:DEPTH]] for scores in similarities]


def run_rankings(portfolio, ranks):
    score = defaultdict(float)
    for source in ranks:
        if source not in VIEW_WEIGHTS or source in portfolio:
            weight = VIEW_WEIGHTS.get(source, 1)
            for rank, index in enumerate(ranks[source], 1):
                score[index] += weight / (35 + rank)
    return sorted(score, key=lambda index: (-score[index], index))


def evaluate(portfolio, questions, rankings):
    hit_at_1, hit_at_5, hit_at_10, rr, recall_at_10 = [], [], [], [], []
    for entry, ranks in zip(questions, rankings, strict=True):
        expected = entry["expected_rows"]
        ranked = run_rankings(portfolio, ranks)[:10]
        first = next((rank for rank, index in enumerate(ranked, 1) if index in expected), None)
        hit_at_1.append(first == 1)
        hit_at_5.append(first is not None and first <= 5)
        hit_at_10.append(first is not None)
        rr.append(1 / first if first else 0)
        recall_at_10.append(len(expected.intersection(ranked)) / len(expected))
    return {
        "queries": len(rr),
        "hit_at_1": round(float(np.mean(hit_at_1)), 4),
        "hit_at_5": round(float(np.mean(hit_at_5)), 4),
        "hit_at_10": round(float(np.mean(hit_at_10)), 4),
        "mrr_at_10": round(float(np.mean(rr)), 4),
        "recall_at_10": round(float(np.mean(recall_at_10)), 4),
    }


def benchmark(corpus_path, labels_path, dense, storage_budget):
    if storage_budget < 0:
        raise ValueError("The extra index byte budget must be nonnegative")
    rows = json.loads(corpus_path.read_text(encoding="utf-8-sig"))
    questions = json.loads(labels_path.read_text(encoding="utf-8-sig"))
    chunk_rows = {row["chunk_id"]: index for index, row in enumerate(rows, 1)}
    for entry in questions:
        entry["expected_rows"] = {chunk_rows[chunk] for chunk in entry["expected_chunk_ids"]}
        if not entry["expected_rows"]:
            raise ValueError("Every question needs at least one labeled chunk")
    partitions = {
        split: [index for index, entry in enumerate(questions) if entry["split"] == split]
        for split in ("development", "heldout")
    }
    if not all(partitions.values()):
        raise ValueError("Labels must include development and heldout questions")
    if set.intersection(*(set(questions[index]["topic"] for index in ids) for ids in partitions.values())):
        raise ValueError("A topic cannot occur in both splits")
    with TemporaryDirectory(prefix="cami-") as directory:
        build = {}
        rankings = [dict() for _ in questions]
        if dense:
            for ranking, ranked in zip(rankings, dense_ranks(rows, questions), strict=True):
                ranking["dense"] = ranked
        for name in ("body", *VIEW_WEIGHTS):
            connection, build[name] = materialize(Path(directory), name, rows)
            try:
                elapsed = []
                for entry, ranking in zip(questions, rankings, strict=True):
                    started = perf_counter()
                    ranking[name] = retrieve(connection, entry["query"], name)
                    elapsed.append(1000 * (perf_counter() - started))
                build[name]["lookup_p95_ms"] = round(float(np.percentile(elapsed, 95)), 2)
            finally:
                connection.close()
    portfolios = [
        config for size in range(len(VIEW_WEIGHTS) + 1)
        for config in combinations(VIEW_WEIGHTS, size)
    ]
    trials = []
    reference_questions = [index for index in partitions["heldout"] if reference_codes(questions[index]["query"])]
    if reference_questions:
        partitions["heldout_references"] = reference_questions
    for portfolio in portfolios:
        outcomes = {
            split: evaluate(portfolio, [questions[index] for index in ids], [rankings[index] for index in ids])
            for split, ids in partitions.items()
        }
        trials.append({
            "views": portfolio,
            "extra_bytes": sum(build[view]["bytes"] for view in portfolio),
            "extra_ms": round(sum(build[view]["milliseconds"] for view in portfolio), 1),
            **outcomes,
        })
    chosen = max(
        (trial for trial in trials if trial["extra_bytes"] <= storage_budget),
        key=lambda trial: (trial["development"]["hit_at_5"], -trial["extra_bytes"], trial["development"]["mrr_at_10"]),
    )
    deltas = {}
    for split, indexes in partitions.items():
        improved, regressed = [], []
        for index in indexes:
            entry = questions[index]
            ranks = rankings[index]
            baseline = bool(entry["expected_rows"].intersection(run_rankings((), ranks)[:5]))
            promoted = bool(entry["expected_rows"].intersection(run_rankings(chosen["views"], ranks)[:5]))
            if promoted and not baseline:
                improved.append(entry["topic"])
            elif baseline and not promoted:
                regressed.append(entry["topic"])
        deltas[split] = {"improved": improved, "regressed": regressed}
    return {
        "rows": len(rows),
        "storage_budget": storage_budget,
        "dense": dense,
        "build": build,
        "trials": trials,
        "selected_on_development": chosen["views"],
        "selected_deltas": deltas,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path, help="JSON export with document_id, chunk_id, chunk_index, text and vector")
    parser.add_argument("labels", type=Path, help="JSON records with query, topic, split and expected_chunk_ids")
    parser.add_argument("--dense", action="store_true", help="Include exact BGE retrieval in the baseline")
    parser.add_argument("--extra-index-bytes", type=int, default=256 * 1024)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.corpus, args.labels, args.dense, args.extra_index_bytes), ensure_ascii=False))


if __name__ == "__main__":
    main()
