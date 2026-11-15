"""Local, administrator-reviewed examples for the main agent's routing decision."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import unicodedata
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import time
from uuid import uuid4

from .contracts import AgentId
from .routing_similarity import EmbeddingProvider, SemanticRouting


DATA_DIR_ENV = "CLOUD_ROUTING_DATA_DIR"
MAX_INTRODUCTION_CHARS = 1200
MAX_PROMPT_CHARS = 800
MAX_PENDING_SAMPLES = 1000
MAX_REVIEWED_EXAMPLES = 300
ROUTING_TIMEZONE = timezone(timedelta(hours=8))
_DIRECT_ANSWER = "direct"
_MASK = "〈已脱敏〉"
_PRIVATE_TEXT = (
    re.compile(r"(?:https?://|www\.)[^\s<>,，。;；！、]+", re.IGNORECASE),
    re.compile(r"(?<![A-Z0-9._%+\-])[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE),
    re.compile(
        r"(?:姓名|联系人|用户名|账号|学号|工号|手机号|身份证(?:号)?|证件号|邮箱|住址|家庭地址|收件地址|"
        r"密码|口令|验证码|api[ _-]?key|(?:access|refresh)[ _-]?token|email|password|secret|"
        r"full[ _-]?name|home[ _-]?address)" r"[\"']?\s*(?:是|为|[:=]|\bis\b)\s*"
        r"""(?:"[^"\n]*"|'[^'\n]*'|[^\n,，;；。！？]+)""",
        re.IGNORECASE,
    ),
    re.compile(r"(?:我叫|我的名字(?:是|叫))\s*[\u3400-\u9fff]{2,4}"),
    re.compile(r"(?:bearer\s+[A-Z0-9._~+/=\-]+|(?:sk-|ghp_)[A-Z0-9_\-]{8,})", re.IGNORECASE),
    re.compile(r"(?:\d{1,3}\.){3}\d{1,3}|\+?\d[\d. ()\-]{4,}\d[Xx]?"),
)


def _anonymous_prompt(prompt: str) -> str:
    text = unicodedata.normalize("NFKC", prompt)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    for pattern in _PRIVATE_TEXT:
        text = pattern.sub(_MASK, text)
    return " ".join(text.split())[:MAX_PROMPT_CHARS]


def _match_text(prompt: str) -> str:
    return "".join(char for char in prompt.replace(_MASK, "").casefold() if char.isalnum())


def _bigrams(text: str) -> set[str]:
    return {text[index:index + 2] for index in range(len(text) - 1)}


def _suggest(
    prompt: str, examples: list[tuple[str, str]], semantic: SemanticRouting | None = None,
) -> tuple[str | None, str | None]:
    text = _match_text(prompt)
    if not text:
        return None, None
    terms = _bigrams(text)
    exact, similar = set(), set()
    for example, expected in examples:
        other = _match_text(example)
        if text == other:
            exact.add(expected)
        elif len(terms) >= 5:
            other_terms = _bigrams(other)
            if len(other_terms) >= 5:
                similarity = len(terms & other_terms) / (len(terms) * len(other_terms)) ** 0.5
                if similarity >= 0.70:
                    similar.add(expected)
    labels = exact or similar
    if labels:
        if len(labels) != 1:
            return None, "conflict"
        label, method = next(iter(labels)), "exact" if exact else "similar"
    elif semantic is not None:
        label = semantic.suggest(
            prompt.replace(_MASK, " "), [(text.replace(_MASK, " "), target) for text, target in examples],
        )
        method = "semantic"
    else:
        return None, None
    if label is None:
        return None, None
    return label, method


@dataclass(frozen=True)
class RoutingAdvice:
    introductions: dict[AgentId, str]
    suggested_targets: tuple[AgentId, ...] | None
    method: str | None = None
    enabled: bool = False
    suggest_direct: bool = False


class RoutingFeedback:
    """Stores only redacted prompts, dispatched roles and administrator labels."""

    def __init__(self, directory: str | Path, *, embed: EmbeddingProvider | None = None):
        directory = Path(directory).expanduser()
        if not directory.is_absolute() or directory.drive.startswith("\\\\"):
            raise ValueError(f"{DATA_DIR_ENV} 必须是本机目录的绝对路径")
        directory = directory.resolve()
        if not directory.is_dir():
            raise ValueError(f"请先创建 {DATA_DIR_ENV} 指定的本机目录")
        if os.name != "nt" and directory.stat().st_mode & 0o077:
            raise ValueError(f"{DATA_DIR_ENV} 必须是服务账号独占的目录（权限 0700）")
        self.path = directory / "routing.sqlite3"
        self._uri = f"{self.path.as_uri()}?mode=rw"
        self._semantic = SemanticRouting(embed) if embed is not None else None

    def initialize(self) -> None:
        # Run as the service account so local administration never changes file ownership.
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with self._connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS routing_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1), epoch TEXT
            )""")
            db.execute("INSERT OR IGNORE INTO routing_settings (id) VALUES (1)")
            db.execute("""CREATE TABLE IF NOT EXISTS routing_introductions (
                agent_id TEXT PRIMARY KEY, introduction TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS routing_samples (
                id TEXT PRIMARY KEY, prompt TEXT NOT NULL, called_agents TEXT NOT NULL,
                explicit INTEGER NOT NULL CHECK (explicit IN (0, 1)),
                score INTEGER CHECK (score BETWEEN 1 AND 5), expected_targets TEXT, review_order INTEGER,
                created_at INTEGER,
                answered_direct INTEGER NOT NULL DEFAULT 0 CHECK (answered_direct IN (0, 1)),
                expected_direct INTEGER NOT NULL DEFAULT 0 CHECK (expected_direct IN (0, 1))
            )""")
            columns = {column[1] for column in db.execute("PRAGMA table_info(routing_samples)")}
            if "created_at" not in columns:
                db.execute("ALTER TABLE routing_samples ADD COLUMN created_at INTEGER")
            if "answered_direct" not in columns:
                db.execute("ALTER TABLE routing_samples ADD COLUMN answered_direct INTEGER NOT NULL DEFAULT 0"
                           " CHECK (answered_direct IN (0, 1))")
            if "expected_direct" not in columns:
                db.execute("ALTER TABLE routing_samples ADD COLUMN expected_direct INTEGER NOT NULL DEFAULT 0"
                           " CHECK (expected_direct IN (0, 1))")

    @contextmanager
    def _connect(self, *, write=False):
        if self.path.is_symlink() or (os.name != "nt" and self.path.stat().st_mode & 0o077):
            raise PermissionError("路由数据库必须是本机私有文件")
        with closing(sqlite3.connect(self._uri, uri=True, timeout=1.0)) as db, db:
            if write:
                db.execute("PRAGMA secure_delete = ON")
                db.execute("BEGIN IMMEDIATE")
            else:
                db.execute("BEGIN")
            yield db

    @staticmethod
    def _epoch(db) -> str | None:
        row = db.execute("SELECT epoch FROM routing_settings WHERE id = 1").fetchone()
        return row[0] if row else None

    def _read_epoch(self) -> str | None:
        with self._connect() as db:
            return self._epoch(db)

    def is_enabled(self) -> bool:
        return self._read_epoch() is not None

    def set_enabled(self, enabled: bool) -> None:
        with self._connect(write=True) as db:
            if enabled:
                db.execute("UPDATE routing_settings SET epoch = COALESCE(epoch, ?) WHERE id = 1", (uuid4().hex,))
            else:
                db.execute("UPDATE routing_settings SET epoch = NULL WHERE id = 1")

    @staticmethod
    def _read_introductions(db) -> dict[AgentId, str]:
        rows = db.execute("SELECT agent_id, introduction FROM routing_introductions")
        return {AgentId(role): introduction for role, introduction in rows}

    def introductions(self) -> dict[AgentId, str]:
        with self._connect() as db:
            return self._read_introductions(db)

    def set_introduction(self, role: AgentId, introduction: str) -> None:
        if not isinstance(role, AgentId):
            raise ValueError("请选择已注册的角色")
        introduction = introduction.strip() if isinstance(introduction, str) else ""
        if not 1 <= len(introduction) <= MAX_INTRODUCTION_CHARS:
            raise ValueError(f"自我介绍必须包含 1 到 {MAX_INTRODUCTION_CHARS} 个字符")
        with self._connect(write=True) as db:
            db.execute("""INSERT INTO routing_introductions (agent_id, introduction) VALUES (?, ?)
                ON CONFLICT (agent_id) DO UPDATE SET introduction = excluded.introduction""",
                       (role.value, introduction))

    def reset_introduction(self, role: AgentId) -> bool:
        if not isinstance(role, AgentId):
            raise ValueError("请选择已注册的角色")
        with self._connect(write=True) as db:
            return db.execute("DELETE FROM routing_introductions WHERE agent_id = ?", (role.value,)).rowcount == 1

    def overview(self, days: int = 14) -> dict:
        if type(days) is not int or not 1 <= days <= 90:
            raise ValueError("趋势范围需为 1 到 90 天")
        today = datetime.now(ROUTING_TIMEZONE).date()
        start = today - timedelta(days=days - 1)
        timeline = {(start + timedelta(days=index)).isoformat(): {"total": 0, "reviewed": 0} for index in range(days)}
        actual_counts, expected_counts = Counter(), Counter()
        reviewed = agreed = undated = 0
        with self._connect() as db:
            enabled = self._epoch(db) is not None
            introductions = self._read_introductions(db)
            rows = db.execute("""SELECT called_agents, answered_direct, expected_targets, expected_direct, score, created_at
                FROM routing_samples""").fetchall()
        total = len(rows)
        for called, answered_direct, expected, expected_direct, score, created_at in rows:
            actual = {_DIRECT_ANSWER} if answered_direct else set(json.loads(called))
            actual_counts.update(actual or (None,))
            if score is not None:
                reviewed += 1
                targets = {_DIRECT_ANSWER} if expected_direct else set(json.loads(expected or "[]"))
                agreed += actual == targets
                expected_counts.update(targets or (None,))
            if created_at is None:
                undated += 1
                continue
            day = datetime.fromtimestamp(created_at, ROUTING_TIMEZONE).date().isoformat()
            if day in timeline:
                timeline[day]["total"] += 1
                timeline[day]["reviewed"] += score is not None
        return {
            "enabled": enabled,
            "introductions": introductions,
            "totals": {"total": total, "reviewed": reviewed, "pending": total - reviewed, "agreed": agreed,
                       "undated": undated, "active_examples": min(reviewed, MAX_REVIEWED_EXAMPLES) if enabled else 0},
            "timeline": [{"day": day, **values} for day, values in timeline.items()],
            "distribution": [{"agent": role, "actual": actual_counts[role], "expected": expected_counts[role]}
                             for role in (*AgentId, _DIRECT_ANSWER, None)],
        }

    def samples_page(
        self, limit: int = 20, offset: int = 0, *, status: str = "all",
        agent: str = "", origin: str = "", q: str = "", day: str = "",
    ) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("每次可查看 1 到 100 条样本")
        if type(offset) is not int or offset < 0:
            raise ValueError("分页位置无效")
        clauses, params = [], []
        if status == "pending":
            clauses.append("score IS NULL")
        elif status == "reviewed":
            clauses.append("score IS NOT NULL")
        elif status == "different":
            clauses.append("""score IS NOT NULL AND (
                answered_direct != expected_direct
                OR EXISTS (SELECT value FROM json_each(called_agents) EXCEPT SELECT value FROM json_each(expected_targets))
                OR EXISTS (SELECT value FROM json_each(expected_targets) EXCEPT SELECT value FROM json_each(called_agents))
            )""")
        elif status != "all":
            raise ValueError("样本状态筛选无效")
        if agent == "none":
            clauses.append("json_array_length(called_agents) = 0")
        elif agent == "undispatched":
            clauses.append("answered_direct = 0 AND json_array_length(called_agents) = 0")
        elif agent == _DIRECT_ANSWER:
            clauses.append("answered_direct = 1")
        elif agent:
            clauses.append("EXISTS (SELECT 1 FROM json_each(called_agents) WHERE value = ?)")
            params.append(AgentId(agent).value)
        if origin:
            source = {"automatic": 0, "preferred": 1}.get(origin)
            if source is None:
                raise ValueError("样本来源筛选无效")
            clauses.append("explicit = ?")
            params.append(source)
        if search := unicodedata.normalize("NFKC", q).strip():
            clauses.append("(instr(lower(prompt), lower(?)) > 0 OR instr(id, lower(?)) > 0)")
            params.extend((search, search))
        if day:
            start = int(datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=ROUTING_TIMEZONE).timestamp())
            clauses.append("created_at >= ? AND created_at < ?")
            params.extend((start, start + 86400))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            total = db.execute("SELECT COUNT(*) FROM routing_samples" + where, params).fetchone()[0]
            if offset >= total:
                offset = max(0, (total - 1) // limit * limit)
            rows = db.execute(
                "SELECT id, prompt, called_agents, answered_direct, explicit, score, expected_targets, expected_direct,"
                " created_at FROM routing_samples"
                + where + " ORDER BY score IS NOT NULL, rowid DESC LIMIT ? OFFSET ?", (*params, limit, offset),
            ).fetchall()
        samples = [
            {"id": sample_id, "prompt": prompt, "called_agents": json.loads(called),
             "answered_direct": bool(answered_direct), "explicit": bool(explicit), "score": score,
             "expected_targets": json.loads(expected) if expected is not None else None,
             "expected_direct": bool(expected_direct), "created_at": created_at}
            for sample_id, prompt, called, answered_direct, explicit, score, expected, expected_direct, created_at in rows
        ]
        next_offset = offset + limit if offset + limit < total else None
        return {"samples": samples, "total": total, "offset": offset, "next_offset": next_offset}

    def list_samples(self, limit: int = 20) -> list[dict]:
        return self.samples_page(limit)["samples"]

    def grade(self, sample_id: str, score: int, expected_targets: tuple[AgentId, ...], *, direct: bool = False) -> bool:
        if type(score) is not int or not 1 <= score <= 5:
            raise ValueError("评分必须是 1 到 5 的整数")
        if any(not isinstance(role, AgentId) for role in expected_targets) or len(set(expected_targets)) != len(expected_targets):
            raise ValueError("正确角色必须是已注册且不重复的角色")
        if type(direct) is not bool or (direct and expected_targets):
            raise ValueError("直接回答不能同时指定子智能体")
        expected = json.dumps(sorted(role.value for role in expected_targets))
        with self._connect(write=True) as db:
            result = db.execute("""UPDATE routing_samples SET score = ?, expected_targets = ?, expected_direct = ?,
                review_order = (SELECT COALESCE(MAX(review_order), 0) + 1 FROM routing_samples)
                WHERE id = ?""", (score, expected, direct, sample_id))
            return result.rowcount == 1

    def delete_sample(self, sample_id: str) -> bool:
        return self.delete_samples((sample_id,)) == 1

    def clear_grade(self, sample_id: str) -> bool:
        with self._connect(write=True) as db:
            return db.execute("""UPDATE routing_samples
                SET score = NULL, expected_targets = NULL, expected_direct = 0, review_order = NULL WHERE id = ?""",
                              (sample_id,)).rowcount == 1

    def delete_samples(self, sample_ids: tuple[str, ...]) -> int:
        with self._connect(write=True) as db:
            return db.executemany("DELETE FROM routing_samples WHERE id = ?", ((sample_id,) for sample_id in sample_ids)).rowcount

    def _read_advice(self, prompt: str) -> RoutingAdvice:
        with self._connect() as db:
            introductions = self._read_introductions(db)
            epoch = self._epoch(db)
            if epoch is None:
                return RoutingAdvice(introductions, None)
            examples = db.execute("""SELECT prompt, CASE WHEN expected_direct = 1 THEN ? ELSE expected_targets END
                FROM routing_samples WHERE expected_targets IS NOT NULL ORDER BY review_order DESC LIMIT ?""",
                                  (_DIRECT_ANSWER, MAX_REVIEWED_EXAMPLES)).fetchall()
        label, method = _suggest(_anonymous_prompt(prompt), examples, self._semantic)
        if method is not None and (current := self._read_epoch()) != epoch:
            return RoutingAdvice(introductions, None, enabled=current is not None)
        direct = label == _DIRECT_ANSWER
        targets = tuple(AgentId(value) for value in json.loads(label)) if label is not None and not direct else None
        return RoutingAdvice(introductions, targets, method, enabled=True, suggest_direct=direct)

    def preview(self, prompt: str) -> dict:
        advice = self._read_advice(prompt)
        return {"enabled": advice.enabled, "suggested_targets": advice.suggested_targets,
                "suggest_direct": advice.suggest_direct, "method": advice.method}

    def _record(
        self, epoch: str, prompt: str, called_agents: tuple[AgentId, ...], explicit: bool, answered_direct: bool = False,
    ) -> None:
        prompt = _anonymous_prompt(prompt)
        if not _match_text(prompt):
            return
        with self._connect(write=True) as db:
            if not epoch or epoch != self._epoch(db):
                return
            called = json.dumps([role.value for role in called_agents])
            direct = answered_direct and not called_agents
            db.execute("""INSERT INTO routing_samples (id, prompt, called_agents, answered_direct, explicit, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""", (uuid4().hex, prompt, called, direct, explicit, int(time())))
            db.execute("""DELETE FROM routing_samples WHERE rowid IN (
                SELECT rowid FROM routing_samples WHERE score IS NULL ORDER BY rowid DESC LIMIT -1 OFFSET ?
            )""", (MAX_PENDING_SAMPLES,))

    @staticmethod
    async def _runtime(operation, *args):
        try:
            return await asyncio.to_thread(operation, *args)
        except (OSError, sqlite3.Error, ValueError) as exc:
            logging.getLogger(__name__).warning("Local routing feedback unavailable (%s)", type(exc).__name__)
            return None

    async def advice(self, prompt: str) -> RoutingAdvice | None:
        return await self._runtime(self._read_advice, prompt)

    async def recording_epoch(self) -> str | None:
        return await self._runtime(self._read_epoch)

    async def record(
        self, epoch: str, prompt: str, called_agents: tuple[AgentId, ...], *, explicit: bool, answered_direct: bool = False,
    ) -> None:
        await self._runtime(self._record, epoch, prompt, called_agents, explicit, answered_direct)
