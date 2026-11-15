"""A rebuildable, scoped FTS5 index of the local Milvus collection."""

import os
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Iterable

if __package__:
    from .rag_ranking import _normalize, _TERM_RUN
else:
    from rag_ranking import _normalize, _TERM_RUN


_VERSION = 3
_CHUNK_ID = re.compile(r"[0-9a-f]{64}")
_REFERENCE = re.compile(
    r"(?P<issuer>[\u3400-\u9fff]{2,12}字\s*)?"
    r"[\[(〔【]\s*(?P<year>(?:19|20)\d{2})\s*[\])〕】]\s*(?:第\s*)?(?P<number>\d{1,6})\s*号"
)
_QUERY_TERMS = 48
_LOOKUP_SECONDS = 0.20


def _scope_key(scope: str) -> str:
    # FTS5 case folds its tokens; Milvus scope names are case-sensitive.
    return "s" + scope.encode("utf-8").hex()


def lexical_tokens(text: str) -> list[str]:
    result: list[str] = []
    for part in _TERM_RUN.finditer(_normalize(text)):
        value = part[0]
        if part.lastgroup == "han" and len(value) >= 2:
            result.extend(value[i:i + 2] for i in range(len(value) - 1))
        else:
            result.append(value)
    return result


def reference_mentions(text: str):
    """Recognize a publication reference even if input uses different brackets or zero padding."""
    return tuple((match[0], f"ref{match['year']}x{int(match['number'])}", (match['issuer'] or '').strip())
                 for match in _REFERENCE.finditer(_normalize(text)))


def _fts_expression(scope: str, query: str, *, phrase: bool) -> str | None:
    tokens = lexical_tokens(query)[:_QUERY_TERMS]
    if not tokens:
        return None
    if phrase:
        terms = '"' + ' '.join(tokens) + '"'
    else:
        # The tokeniser emits word characters only. Quote even keywords such as AND/OR.
        terms = ' OR '.join(f'"{term}"' for term in dict.fromkeys(tokens) if len(term) > 1)
        if not terms:
            return None
    return f'scope_key : "{_scope_key(scope)}" AND {{title body}} : ({terms})'


class LexicalIndex:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def _connect(self, mode: str):
        uri = f"{self.path.resolve().as_uri()}?mode={mode}"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as db, db:
            yield db

    @staticmethod
    def _ready(db: sqlite3.Connection) -> bool:
        return db.execute("SELECT ready FROM lexical_meta WHERE version = ?", (_VERSION,)).fetchone() == (1,)

    @contextmanager
    def reader(self):
        if not self.path.is_file():
            yield None
            return
        with self._connect("ro") as db:
            yield LexicalReader(db) if self._ready(db) else None

    def invalidate(self) -> bool:
        """Commit the dirty marker before any Milvus mutation. False means no ready index."""
        if not self.path.is_file():
            return False
        with self._connect("rw") as db:
            if not self._ready(db):
                return False
            db.execute("UPDATE lexical_meta SET ready = 0")
        return True

    def replace_document(self, scope: str, document_id: str, rows: Iterable[dict]) -> None:
        with self._connect("rw") as db:
            key = (_scope_key(scope), document_id)
            affected = "SELECT rowid FROM lexical_rows WHERE scope_key = ? AND document_id = ?"
            db.execute(f"DELETE FROM lexical_fts WHERE rowid IN ({affected})", key)
            db.execute(f"DELETE FROM lexical_reference WHERE scope_key = ? AND chunk_row IN ({affected})", (key[0], *key))
            db.execute("DELETE FROM lexical_rows WHERE scope_key = ? AND document_id = ?", key)
            for row in rows:
                self._insert(db, row)
            db.execute("UPDATE lexical_meta SET ready = 1")

    @staticmethod
    def _insert(db: sqlite3.Connection, row: dict) -> None:
        scope_key = _scope_key(row["scope"])
        cursor = db.execute(
            "INSERT INTO lexical_rows(chunk_id, scope_key, document_id) VALUES (?, ?, ?)",
            (row["chunk_id"], scope_key, row["document_id"]),
        )
        db.execute("INSERT INTO lexical_fts(rowid, scope_key, title, body) VALUES (?, ?, ?, ?)", (
            cursor.lastrowid, scope_key, ' '.join(lexical_tokens(row["source"])), ' '.join(lexical_tokens(row["text"])),
        ))
        db.executemany("INSERT INTO lexical_reference(scope_key, code, chunk_row) VALUES (?, ?, ?)", (
            (scope_key, code, cursor.lastrowid) for code in dict.fromkeys(item[1] for item in reference_mentions(row["text"]))
        ))

    def rebuild(self, rows: Iterable[dict]) -> int:
        # Keep the last usable file until the replacement index has fully committed.
        with TemporaryDirectory(dir=self.path.parent, prefix=".rag-lexical-") as temp:
            path = Path(temp, "index.sqlite3")
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript("""
                    CREATE TABLE lexical_meta(version INTEGER PRIMARY KEY, ready INTEGER NOT NULL);
                    CREATE TABLE lexical_rows(rowid INTEGER PRIMARY KEY, chunk_id TEXT NOT NULL,
                                              scope_key TEXT NOT NULL, document_id TEXT NOT NULL);
                    CREATE INDEX lexical_document ON lexical_rows(scope_key, document_id);
                    CREATE TABLE lexical_reference(scope_key TEXT NOT NULL, code TEXT NOT NULL, chunk_row INTEGER NOT NULL,
                                                   PRIMARY KEY(scope_key, code, chunk_row)) WITHOUT ROWID;
                    CREATE INDEX lexical_reference_chunk ON lexical_reference(scope_key, chunk_row);
                """)
                # Modern SQLite can maintain the index without keeping a second copy of the token stream.
                storage = ", content='', contentless_delete=1" if sqlite3.sqlite_version_info >= (3, 43, 0) else ""
                db.execute(f"CREATE VIRTUAL TABLE lexical_fts USING fts5(scope_key, title, body, tokenize='unicode61'{storage})")
                count = 0
                for row in rows:
                    self._insert(db, row)
                    count += 1
                db.execute("INSERT INTO lexical_fts(lexical_fts) VALUES ('optimize')")
                db.execute("INSERT INTO lexical_meta VALUES (?, 1)", (_VERSION,))
            os.replace(path, self.path)
        return count


class LexicalReader:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def lookup(self, scope: str, query: str, *, phrase: bool = False, limit: int) -> list[str]:
        match = _fts_expression(scope, query, phrase=phrase)
        if match is None:
            return []
        deadline = monotonic() + _LOOKUP_SECONDS
        self._db.set_progress_handler(lambda: monotonic() >= deadline, 1_000)
        try:
            scope_key = _scope_key(scope)
            fts = self._db.execute(
                "SELECT m.chunk_id FROM lexical_fts JOIN lexical_rows AS m ON m.rowid = lexical_fts.rowid "
                "WHERE lexical_fts MATCH ? AND m.scope_key = ? ORDER BY bm25(lexical_fts, 0, 1.6, 1), m.rowid LIMIT ?",
                (match, scope_key, limit),
            ).fetchall()
            codes = tuple(dict.fromkeys(mention[1] for mention in reference_mentions(query)))[:2] if phrase else ()
            references = []
            if codes:
                placeholders = ','.join('?' for _ in codes)
                references = self._db.execute(
                    "SELECT m.chunk_id FROM lexical_reference AS r JOIN lexical_rows AS m ON r.chunk_row = m.rowid "
                    f"WHERE r.scope_key = ? AND r.code IN ({placeholders}) ORDER BY r.code, r.chunk_row LIMIT ?",
                    (scope_key, *codes, limit),
                ).fetchall()
            return list(dict.fromkeys(row[0] for row in (*fts, *references) if _CHUNK_ID.fullmatch(row[0])))[:limit]
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT:
                return []
            raise
        finally:
            self._db.set_progress_handler(None, 0)
