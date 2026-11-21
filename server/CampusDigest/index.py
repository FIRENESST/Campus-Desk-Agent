"""Persist a searchable copy of the digest package's published query results."""

import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from threading import Condition
from urllib.parse import parse_qs, urlencode, urlsplit


_LOG = logging.getLogger(__name__)
_SOURCES = {"data/items.json": "items", "data/weread_items.json": "items", "data/official_posts.json": "accounts"}
_INDEX_FORMAT = 4
MAX_ITEMS = 5000
MAX_ARTICLE_TEXT = 48_000
_ACCOUNT_REGISTRY = "config/official_accounts.json"
_ARTICLE_ARCHIVE = "data/mp_articles.json"
UNAVAILABLE_ARTICLE = {"文章已被删除", "该内容已被发布者删除", "内容已被删除", "该内容因违规无法查看", "此内容无法查看"}
ARTICLE_CHALLENGE = re.compile(r"访问过于频繁|操作频繁|环境异常|登录超时|请使用微信.*打开|微信公众平台安全验证")
_BEIJING_TIME = timezone(timedelta(hours=8))
_ORDER = "published_on DESC, published_at DESC, id"
_RELEVANCE_CANDIDATES = 64
_PUBLIC_COLUMNS = "id,source,title,publisher,published_on,published_at,url,source_id,excerpt,registration_deadline"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS digest_revision (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    source_stamp TEXT NOT NULL, content_hash TEXT NOT NULL, version INTEGER NOT NULL,
    updated_at TEXT NOT NULL, latest_published_on TEXT, item_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS digest_item (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, publisher TEXT NOT NULL,
    published_on TEXT, published_at TEXT, url TEXT, source_id TEXT, excerpt TEXT NOT NULL, registration_deadline TEXT,
    search_text TEXT NOT NULL, publisher_search TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS digest_wechat_by_title ON digest_item(published_on, title) WHERE source='wechat';
CREATE TABLE IF NOT EXISTS digest_poll_state (source TEXT PRIMARY KEY, progress TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS digest_wechat_account (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS digest_wechat_body (
    article_id TEXT PRIMARY KEY REFERENCES digest_item(id) ON DELETE CASCADE,
    content TEXT NOT NULL, search_body TEXT NOT NULL, content_source TEXT NOT NULL, canonical_url TEXT
);
"""
_ITEM_COLUMNS = ("id", "source", "title", "publisher", "published_on", "published_at", "url", "source_id",
                 "excerpt", "registration_deadline", "search_text", "publisher_search")
_SAME_WECHAT_TITLE = "SELECT * FROM digest_item WHERE source='wechat' AND published_on IS ? AND title=?"
_SAVE_ITEM = (
    f"INSERT INTO digest_item ({','.join(_ITEM_COLUMNS)}) "
    f"VALUES ({','.join(':' + column for column in _ITEM_COLUMNS)}) ON CONFLICT(id) DO UPDATE SET "
    + ",".join(f"{column}=excluded.{column}" for column in _ITEM_COLUMNS if column != "id")
)


def _normalize(value):
    return unicodedata.normalize("NFKC", value).casefold()


def _account_key(value):
    return " ".join(_normalize(value).split())


@lru_cache(maxsize=256)
def _account_id(name):
    return hashlib.sha256(_account_key(name).encode()).hexdigest() if name else None


def _text(value, length):
    return " ".join(value.split())[:length] if isinstance(value, str) else ""


def _published_on(value):
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            pass
    return None


def _published_at(value):
    if isinstance(value, str) and len(value) >= 16 and value[10] in " T":
        try:
            timestamp = datetime.fromisoformat(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=_BEIJING_TIME)
            return timestamp.astimezone(_BEIJING_TIME).isoformat(timespec="seconds")
        except (OverflowError, ValueError):
            pass
    return None


def _unix_publication(value):
    if type(value) is int or (isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 13):
        try:
            timestamp = int(value)
            if timestamp > 0:
                return datetime.fromtimestamp(
                    timestamp / 1000 if timestamp >= 100_000_000_000 else timestamp, _BEIJING_TIME,
                ).isoformat(timespec="seconds")
        except (OSError, OverflowError, ValueError):
            pass
    return None


def _reference(value):
    if not isinstance(value, str) or len(value) > 2048 or any(char.isspace() for char in value):
        return None, None
    try:
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None, None
    except ValueError:
        return None, None
    if parsed.scheme in {"http", "https"}:
        return value, None
    return (None, value) if parsed.scheme == "weread" else (None, None)


def _url_key(url):
    parsed = urlsplit(url)
    if parsed.hostname == "mp.weixin.qq.com":
        if parsed.path.startswith("/s/"):
            return "https://mp.weixin.qq.com" + parsed.path
        if parsed.path == "/s":
            params = parse_qs(parsed.query)
            if {"__biz", "mid", "idx"} <= params.keys():
                return "https://mp.weixin.qq.com/s?" + urlencode(
                    [(key, params[key][0]) for key in ("__biz", "mid", "idx")],
                )
    return url.split("#", 1)[0]


def _publication_times(items):
    # The upstream list API exposes the date but omits the collected Unix timestamp.
    times = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        url, source_id = _reference(item.get("url"))
        key = _url_key(url) if url else source_id
        timestamp = _unix_publication(item.get("ts")) or _published_at(item.get("published_at") or item.get("date"))
        if key and timestamp:
            title = _text(item.get("title"), 240) if source_id else ""
            times[key, title] = timestamp
    return times


def _official_records(accounts):
    for book_id, account in accounts.items():
        for post in account.get("posts", ()):
            link = post.get("url")
            if not link and (review_id := post.get("reviewId")):
                link = f"weread://{book_id}/{review_id}"
            yield {**post, "url": link}


def _identity(item, hint=""):
    url, source_id = item["url"], item["source_id"]
    fields = item["source"], _normalize(item["title"]), item["published_on"], _account_key(item["publisher"])
    key = _url_key(url) if url else hint or source_id or json.dumps(fields, ensure_ascii=False)
    return hashlib.sha256(key.encode()).hexdigest()


def _rekey(row):
    item = dict(row)
    item["id"] = _identity(item)
    return item


def _item(raw, *, official, publication_times=None):
    title = _text(raw.get("title"), 240)
    if not title:
        return None
    url, source_id = _reference(raw.get("link") if official else raw.get("url"))
    if official and raw.get("source_id"):
        source_id = _reference(raw["source_id"])[1] or source_id
    is_wechat = official or source_id is not None or raw.get("kind") == "公众号" or (
        url is not None and urlsplit(url).hostname == "mp.weixin.qq.com"
    )
    source = "wechat" if is_wechat else "web"
    publisher = _text(raw.get("official_name") or raw.get("account"), 120)
    names = {_account_key(name) for field in ("account", "official_name") if (name := _text(raw.get(field), 120))}
    published = (raw.get("time") if official else raw.get("date")) or raw.get("published_on")
    published_at = _unix_publication(raw.get("ts"))
    if not published_at and publication_times:
        if url:
            published_at = publication_times.get((_url_key(url), ""))
        elif source_id:
            published_at = publication_times.get((source_id, title)) or publication_times.get((source_id, ""))
    published_at = published_at or _published_at(raw.get("published_at") or published)
    published_on = published_at[:10] if published_at else _published_on(published)
    excerpt = _text(raw.get("digest") if official else raw.get("excerpt"), 440)
    insight = raw.get("insight")
    deadline = _text(insight.get("截止"), 120) if isinstance(insight, dict) else ""
    item = {
        "source": source, "title": title,
        "publisher": publisher, "published_on": published_on, "published_at": published_at,
        "url": url, "source_id": source_id,
        "excerpt": excerpt, "registration_deadline": deadline or None,
        "search_text": _normalize(" ".join((title, publisher, excerpt))),
        "publisher_search": "\n".join(sorted(names)),
    }
    item["id"] = _identity(item)
    return item


def _merged_item(current, incoming, live):
    item = dict(current)
    for field in ("publisher", "url", "source_id", "excerpt"):
        if incoming[field] and not item[field]:
            item[field] = incoming[field]
    if incoming["published_at"] and (live or not item["published_at"]):
        item["published_on"] = incoming["published_on"]
        item["published_at"] = incoming["published_at"]
    elif not item["published_on"]:
        item["published_on"] = incoming["published_on"]
    item["registration_deadline"] = incoming["registration_deadline"] or item["registration_deadline"]
    item["source"] = "wechat" if "wechat" in (current["source"], incoming["source"]) else "web"
    names = set(item["publisher_search"].splitlines()) | set(incoming["publisher_search"].splitlines())
    item["publisher_search"] = "\n".join(sorted(names))
    item["search_text"] = _normalize(" ".join(item[field] for field in ("title", "publisher", "excerpt")))
    return item


def _find_item(db, item):
    if row := db.execute("SELECT * FROM digest_item WHERE id=?", (item["id"],)).fetchone():
        return row
    if item["source"] != "wechat":
        return None
    names = set(item["publisher_search"].splitlines())
    matches = [
        row for row in db.execute(_SAME_WECHAT_TITLE, (item["published_on"], item["title"]))
        if _can_merge_wechat(item, row, names)
    ]
    return matches[0] if len(matches) == 1 else None


def _can_merge_wechat(item, row, names):
    if item["url"] and row["url"]:
        return _url_key(item["url"]) == _url_key(row["url"])
    if item["published_at"] and row["published_at"] and item["published_at"] != row["published_at"]:
        return False
    if item["source_id"] and row["source_id"]:
        return item["source_id"] == row["source_id"]
    return bool(names.intersection(row["publisher_search"].splitlines()))


def _merge_records(db, records, *, live):
    changed = False
    for item in records:
        current = _find_item(db, item)
        if current is not None:
            item = _merged_item(current, item, live)
            if item == dict(current):
                continue
        db.execute(_SAVE_ITEM, item)
        changed = True
    return changed


def _trim(db):
    db.execute(
        f"DELETE FROM digest_item WHERE id IN (SELECT id FROM digest_item ORDER BY {_ORDER} LIMIT -1 OFFSET ?)",
        (MAX_ITEMS,),
    )


def _revision(db, previous, stamp):
    digest = hashlib.sha256()
    for row in db.execute(f"SELECT {','.join(_ITEM_COLUMNS)} FROM digest_item ORDER BY id"):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode())
    content_hash = digest.hexdigest()
    if previous and previous["content_hash"] == content_hash:
        if previous["source_stamp"] != stamp:
            db.execute("UPDATE digest_revision SET source_stamp=? WHERE singleton=1", (stamp,))
        return previous["version"]
    version = previous["version"] + 1 if previous else 1
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    latest, count = db.execute("SELECT MAX(published_on), COUNT(*) FROM digest_item").fetchone()
    db.execute("INSERT OR REPLACE INTO digest_revision VALUES (1,?,?,?,?,?,?)",
               (stamp, content_hash, version, updated_at, latest, count))
    return version


def _pages(getter, **filters):
    offset = 0
    while True:
        page = getter({**filters, "limit": ["500"], "offset": [str(offset)]})
        batch, total = page["items"], page["total"]
        if not isinstance(batch, list) or not isinstance(total, int) or (not batch and offset < total):
            raise ValueError("日报查询分页无效")
        yield from batch
        offset += len(batch)
        if offset >= total:
            return


def _records(api, publication_times):
    for raw in _pages(api.get_items, days=["0"]):
        if entry := _item(raw, official=False, publication_times=publication_times):
            yield entry
    for raw in _pages(api.get_official):
        if entry := _item(raw, official=True, publication_times=publication_times):
            yield entry


def _old_format(stamp):
    try:
        return json.loads(stamp).get("format") != _INDEX_FORMAT
    except (AttributeError, ValueError):
        return True


def _save_bodies(db, records):
    db.executemany("""
        INSERT INTO digest_wechat_body(article_id,content,search_body,content_source,canonical_url)
        SELECT ?,?,?,?,? WHERE EXISTS (SELECT 1 FROM digest_item WHERE id=? AND source='wechat')
        ON CONFLICT(article_id) DO UPDATE SET content=excluded.content,search_body=excluded.search_body,
            content_source=excluded.content_source,canonical_url=COALESCE(excluded.canonical_url,digest_wechat_body.canonical_url)
        WHERE length(excluded.content)>length(digest_wechat_body.content)
           OR (excluded.content_source<>'collected_article' AND excluded.content<>digest_wechat_body.content)
    """, ((key, content, _normalize(content), source, url, key) for key, content, source, url in records))


def is_article_placeholder(text, title, publisher):
    cleaned = text.strip(" 。！")
    return (not cleaned or cleaned in UNAVAILABLE_ARTICLE or cleaned in {title, publisher, f"{title} {publisher}"}
            or len(cleaned) < 80 and bool(ARTICLE_CHALLENGE.search(cleaned)))


def _import_wechat_assets(db, directory, sources):
    if _ACCOUNT_REGISTRY in sources:
        registry = json.loads((directory / _ACCOUNT_REGISTRY).read_text(encoding="utf-8-sig"))
        _add_wechat_accounts(db, (item.get("name") for item in registry.get("accounts", []) if isinstance(item, dict)))
    bodies = []
    for path in sources:
        if path not in {"data/weread_items.json", _ARTICLE_ARCHIVE} and not path.startswith("out/corpus/"):
            continue
        document = json.loads((directory / path).read_text(encoding="utf-8-sig"))
        rows = document.get("items", []) if isinstance(document, dict) else document
        for raw in rows if isinstance(rows, list) else ():
            if not isinstance(raw, dict):
                continue
            content = raw.get("body")
            if not content and path == "data/weread_items.json":
                saved, count = raw.get("excerpt"), raw.get("chars")
                if isinstance(saved, str) and type(count) is int and 0 < count <= len(saved):
                    content = saved
            if not isinstance(content, str) or not (content := content.replace("\x00", "").strip()[:MAX_ARTICLE_TEXT + 1]):
                continue
            item = _item(raw, official=False)
            if not item or item["source"] != "wechat" or is_article_placeholder(content, item["title"], item["publisher"]):
                continue
            match = _find_item(db, item)
            if match and _normalize(match["title"]) == _normalize(item["title"]):
                bodies.append((match["id"], content, "collected_article", None))
    _save_bodies(db, bodies)


def _add_wechat_accounts(db, names):
    names = {_text(name, 120) for name in names}
    db.executemany("INSERT OR IGNORE INTO digest_wechat_account(id,name) VALUES (?,?)",
                   ((_account_id(name), name) for name in sorted(names) if name))


def _body_excerpt(text, terms):
    normalized = _normalize(text)
    position = min((index for term in terms if (index := normalized.find(term)) >= 0), default=0)
    start = max(0, position - 60)
    return ("…" if start else "") + _text(text[start:start + 300], 300) + ("…" if start + 300 < len(text) else "")


def _search_relevance(title, search_text, terms):
    normalized_title = _normalize(title)
    title_hits = sum(term in normalized_title for term in terms)
    metadata_hits = sum(term in search_text for term in terms)
    return 2 * title_hits + metadata_hits


class DigestIndex:
    def __init__(self, directory, path, api, *, read_only=False):
        self.directory, self.path, self.api = directory, path, api
        self.read_only = read_only
        self._changed = Condition()
        if read_only:
            with closing(self._connect()) as db:
                revision = db.execute("SELECT version FROM digest_revision WHERE singleton=1").fetchone()
            if revision is None:
                raise LookupError("没有可查询的校园日报缓存版本")
            self._version = revision["version"]
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(digest_item)")}
            if {"publisher_search", "published_at"} - columns:
                db.execute("BEGIN")
                if "publisher_search" not in columns:
                    db.execute("ALTER TABLE digest_item ADD COLUMN publisher_search TEXT NOT NULL DEFAULT ''")
                if "published_at" not in columns:
                    db.execute("ALTER TABLE digest_item ADD COLUMN published_at TEXT")
                db.execute("UPDATE digest_revision SET source_stamp='' WHERE singleton=1")
            db.execute(f"CREATE INDEX IF NOT EXISTS digest_item_by_date ON digest_item({_ORDER})")
            db.execute(f"CREATE INDEX IF NOT EXISTS digest_item_by_source_date ON digest_item(source,{_ORDER})")
            db.execute("CREATE INDEX IF NOT EXISTS digest_wechat_by_publisher ON digest_item(publisher) WHERE source='wechat'")
            revision = db.execute("SELECT version FROM digest_revision WHERE singleton=1").fetchone()
            self._version = revision["version"] if revision else 0

    def _connect(self):
        database = f"{self.path.resolve().as_uri()}?mode=ro" if self.read_only else self.path
        db = sqlite3.connect(database, uri=self.read_only, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.create_function("normalize_title", 1, _normalize, deterministic=True)
        db.create_function("article_url_key", 1, _url_key, deterministic=True)
        db.create_function("wechat_account_id", 1, _account_id, deterministic=True)
        return db

    def _sources(self):
        files = [self.directory / name for name in (*_SOURCES, _ACCOUNT_REGISTRY, _ARTICLE_ARCHIVE)]
        files.extend(sorted((self.directory / "out" / "corpus").glob("*.json")))
        for insight in sorted((self.directory / "out" / "insights").glob("*.json")):
            files.extend((insight, self.directory / "out" / "corpus" / insight.name))
        versions = {}
        for path in files:
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            versions[path.relative_to(self.directory).as_posix()] = (stat.st_mtime_ns, stat.st_size)
        return versions

    def _publish_revision(self, db, previous, stamp, changed):
        if previous is None or changed:
            _trim(db)
            version = _revision(db, previous, stamp)
        else:
            version = previous["version"]
            if previous["source_stamp"] != stamp:
                db.execute("UPDATE digest_revision SET source_stamp=? WHERE singleton=1", (stamp,))
        db.commit()
        if version != self._version:
            self._version = version
            self._changed.notify_all()

    def refresh(self):
        """Import or enrich entries from the report package without discarding polled entries."""
        if self.read_only:
            return False
        try:
            with self._changed, closing(self._connect()) as db, db:
                previous = db.execute("SELECT * FROM digest_revision WHERE singleton=1").fetchone()
                sources = self._sources()
                stamp = json.dumps({"format": _INDEX_FORMAT, "wechat_assets": 1, "sources": sources}, sort_keys=True)
                if previous and previous["source_stamp"] == stamp:
                    return True
                publication_times = {}
                for name, field in _SOURCES.items():
                    if name in sources:
                        document = json.loads((self.directory / name).read_text(encoding="utf-8-sig"))
                        rows = document[field]
                        if not isinstance(rows, dict if field == "accounts" else list):
                            raise ValueError("日报数据格式无效")
                        if name == "data/official_posts.json":
                            publication_times.update(_publication_times(_official_records(rows)))
                        else:
                            publication_times.update(_publication_times(rows))
                records = list(_records(self.api, publication_times))
                if self._sources() != sources:
                    return False
                db.execute("BEGIN IMMEDIATE")
                migrate = bool(previous and _old_format(previous["source_stamp"]))
                if migrate:
                    # Keep archived articles while applying the new link identities.
                    records.extend(map(_rekey, db.execute("SELECT * FROM digest_item")))
                    db.execute("DELETE FROM digest_item")
                changed = _merge_records(db, records, live=False)
                _import_wechat_assets(db, self.directory, sources)
                self._publish_revision(db, previous, stamp, changed or migrate)
        except (OSError, sqlite3.Error, ValueError, KeyError, AttributeError, TypeError) as exc:
            _LOG.warning("校园日报索引更新失败（%s）", type(exc).__name__)
            return False
        return True

    def poll_state(self, source):
        with closing(self._connect()) as db:
            row = db.execute("SELECT progress FROM digest_poll_state WHERE source=?", (source,)).fetchone()
        return json.loads(row["progress"]) if row else {}

    def commit_poll(self, source, progress, records=()):
        with self._changed, closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM digest_revision WHERE singleton=1").fetchone()
            changed = _merge_records(db, records, live=True)
            db.execute("INSERT OR REPLACE INTO digest_poll_state VALUES (?,?)", (source, json.dumps(progress, ensure_ascii=False)))
            self._publish_revision(db, previous, previous["source_stamp"] if previous else "", changed)

    def wait_for_revision(self, previous, timeout):
        with self._changed:
            self._changed.wait_for(lambda: self._version != previous, timeout=timeout)
            return self._version

    def wechat_article(self, article_id):
        with closing(self._connect()) as db:
            row = db.execute(f"""
                SELECT {_PUBLIC_COLUMNS}, EXISTS (
                    SELECT 1 FROM digest_item other
                    WHERE other.source='wechat' AND other.source_id=item.source_id AND other.id<>item.id
                ) AS shared_source
                FROM digest_item item WHERE id=? AND source='wechat'
            """, (article_id,)).fetchone()
            return dict(row) if row else None

    def add_wechat_accounts(self, names):
        with closing(self._connect()) as db, db:
            _add_wechat_accounts(db, names)

    def wechat_accounts(self, *, query="", account_id="", limit=30, offset=0):
        fresh = self.refresh()
        with closing(self._connect()) as db:
            rows = db.execute("""
                WITH seen AS (
                    SELECT id,name FROM digest_wechat_account
                    UNION ALL SELECT wechat_account_id(publisher),publisher FROM (
                        SELECT DISTINCT publisher FROM digest_item WHERE source='wechat' AND publisher<>''
                    )
                )
                SELECT id,min(name) AS name FROM seen WHERE (?='' OR id=?) AND instr(normalize_title(name),?)>0
                GROUP BY id ORDER BY name,id LIMIT ? OFFSET ?
            """, (account_id, account_id, _normalize(query.strip()), limit + 1, offset)).fetchall()
            return {"accounts": [dict(row) for row in rows[:limit]], "has_more": len(rows) > limit, "stale": not fresh}

    def article_body(self, article_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT content,content_source,canonical_url AS url FROM digest_wechat_body WHERE article_id=?",
                             (article_id,)).fetchone()
            return dict(row) if row else None

    def save_article_body(self, article_id, content, source, url):
        with closing(self._connect()) as db, db:
            _save_bodies(db, ((article_id, content[:MAX_ARTICLE_TEXT + 1], source, url),))

    def pending_article_bodies(self):
        with closing(self._connect()) as db:
            return db.execute(f"""
                SELECT id,url FROM digest_item WHERE source='wechat' AND url LIKE 'https://mp.weixin.qq.com/%'
                AND NOT EXISTS (SELECT 1 FROM digest_wechat_body WHERE article_id=id) ORDER BY {_ORDER}
            """).fetchall()

    def read(self, *, query=None, source="all", days=30, limit=6, account="", title="", url="", body="", offset=0,
             followed=(), relevance=False):
        fresh = self.refresh()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN")
            revision = db.execute(
                "SELECT version,updated_at,latest_published_on,item_count FROM digest_revision WHERE singleton=1",
            ).fetchone()
            if revision is None:
                raise LookupError("校园日报索引尚未就绪")
            result = {"revision": dict(revision), "stale": not fresh}
            if self.read_only:
                result["collection_paused"] = True
            if query is None:
                return result
            conditions, params = [], []
            if source != "all":
                conditions.append("source=?")
                params.append(source)
            if days:
                conditions.append("published_on>=?")
                params.append((datetime.now(_BEIJING_TIME).date() - timedelta(days=days)).isoformat())
            if name := _account_key(account):
                conditions.append("instr(publisher_search,?)>0")
                params.append(name)
            for term in dict.fromkeys(_normalize(title).split()):
                conditions.append("instr(normalize_title(title),?)>0")
                params.append(term)
            if url:
                if source == "wechat":
                    conditions.append("(article_url_key(COALESCE(url,''))=? OR article_url_key(COALESCE(canonical_url,''))=?)")
                    params.extend((_url_key(url), _url_key(url)))
                else:
                    conditions.append("article_url_key(COALESCE(url,''))=?")
                    params.append(_url_key(url))
            terms = tuple(dict.fromkeys(_normalize(query).split()))
            body_terms = tuple(dict.fromkeys(_normalize(body).split()))
            for term in terms:
                if source == "wechat":
                    conditions.append("(instr(search_text,?)>0 OR instr(search_body,?)>0)")
                    params.extend((term, term))
                else:
                    conditions.append("instr(search_text,?)>0")
                    params.append(term)
            for term in body_terms:
                conditions.append("instr(search_body,?)>0")
                params.append(term)
            relation = "digest_item"
            if source == "wechat":
                relation += " LEFT JOIN digest_wechat_body ON id=article_id"
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            order = _ORDER
            if relevance and terms:
                db.create_function("digest_relevance", 2, lambda title, text: _search_relevance(title, text, terms),
                                   deterministic=True)
                order = f"digest_relevance(title,search_text) DESC,{order}"
                if not followed:
                    candidates = f"SELECT id FROM {relation}{where} ORDER BY {_ORDER} LIMIT ?"
                    where = f" WHERE id IN ({candidates})"
                    params.append(max(_RELEVANCE_CANDIDATES, limit + offset + 1))
            if source == "wechat":
                following = "0"
                follow_params = ()
                if followed:
                    following = "COALESCE(wechat_account_id(publisher) IN (SELECT value FROM json_each(?)),0)"
                    follow_params = (json.dumps(followed),)
                    order = f"followed DESC,{order}"
                excerpts = ",content AS indexed_body,search_body" if terms or body_terms else ""
                rows = db.execute(f"""
                    SELECT {_PUBLIC_COLUMNS},wechat_account_id(publisher) AS account_id, content_source,
                           {following} AS followed,canonical_url{excerpts}
                    FROM {relation}{where}
                    ORDER BY {order} LIMIT ? OFFSET ?
                """, (*follow_params, *params, limit + 1, offset)).fetchall()
                if terms or body_terms:
                    indexed, total = db.execute("""
                        SELECT (SELECT count(*) FROM digest_wechat_body
                                CROSS JOIN digest_item ON id=article_id WHERE source='wechat'),
                               (SELECT count(*) FROM digest_item WHERE source='wechat')
                    """).fetchone()
                    result["body_coverage"] = {"indexed": indexed, "total": total}
            else:
                rows = db.execute(f"SELECT {_PUBLIC_COLUMNS} FROM digest_item{where} ORDER BY {order} LIMIT ? OFFSET ?",
                                  (*params, limit + 1, offset)).fetchall()
            items = []
            for row in rows[:limit]:
                item = dict(row)
                indexed_body = item.pop("indexed_body", None)
                search_body = item.pop("search_body", None)
                canonical = item.pop("canonical_url", None)
                if source == "wechat":
                    item["followed"] = bool(item["followed"])
                    item["url"] = canonical or item["url"]
                if indexed_body and (matching := [term for term in (*body_terms, *terms) if term in search_body]):
                    item["body_excerpt"] = _body_excerpt(indexed_body, matching)
                items.append(item)
            return {**result, "items": items, "has_more": len(rows) > limit}
