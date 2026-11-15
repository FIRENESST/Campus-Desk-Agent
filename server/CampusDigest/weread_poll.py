"""Use the existing WeRead registry, login and rate-limit handling for list polling."""

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import replace
from datetime import datetime
from functools import partial
from threading import Lock
from time import time

from .collection import _read_json
from .index import _identity, _item
from .polling import OVERLAP, Entry, Page, PollUnavailable, Source, resume_anchors, source_key


_LOG = logging.getLogger(__name__)
_SHELF = "https://weread.qq.com/web/shelf/sync?userVid=&synckey=0"
WEREAD_POLL_SECONDS = 5 * 60
_CACHE_FLUSH_SECONDS = 60
_GLOBAL_STATE = "weread:shared"


def _article(article, book_id, account, registered, aliases=()):
    review_id = article.get("reviewId")
    link = f"weread://{book_id}/{review_id}" if review_id else ""
    item = _item({**article, "account": account, "official_name": registered,
                  "link": article.get("url") or link, "source_id": link}, official=True)
    if item is None:
        return None
    references = (*aliases, link) if link else aliases
    return Entry(item["id"], item, tuple(dict.fromkeys(references)))


def _distinct_articles(entries):
    # Some batches expose only one review ID for several articles.
    counts = Counter(entry.item["source_id"] for entry in entries if entry.item["source_id"])
    records = []
    for entry in entries:
        if entry.item["url"] or counts[entry.item["source_id"]] < 2:
            records.append(entry)
            continue
        key = _identity(entry.item, f"{entry.key}/{entry.item['title']}")
        records.append(replace(entry, key=key, item={**entry.item, "id": key}))
    return tuple(records)


class LegacyOfficialCache:
    """Keep the package's existing daily report and official-feed API supplied."""

    def __init__(self, official):
        self.official = official
        self._lock = Lock()
        self._pages = {}
        self._due = 0

    def accept(self, target, offset, page):
        if offset == 0 and page["articles"]:
            with self._lock:
                self._pages[target["bookId"]] = target, page

    def flush(self, force=False):
        with self._lock:
            if not self._pages or (not force and time() < self._due):
                return
            official = self.official
            posts = _read_json(official.POSTS, {})
            state = {"accounts": {}}
            for target, page in self._pages.values():
                official.apply_page(posts, state, target, 2, 0, page)
            posts["updated_at"] = datetime.now().isoformat(timespec="seconds")
            official.save_json(official.POSTS, posts)
            self._pages.clear()
            self._due = time() + _CACHE_FLUSH_SECONDS


class WeReadPoll:
    def __init__(self, index, official, pool, shelf_request, legacy_cache):
        self.index, self.official, self.pool = index, official, pool
        self.shelf_request, self.legacy_cache = shelf_request, legacy_cache
        self.sources = ()
        self._shelf_due = 0
        self._credentials = ()
        self._usable_accounts = set()
        previous = official.load_json(official.STATE, {})
        self._registry_state = {"learned": previous.get("learned", {})}
        self._shared = index.poll_state(_GLOBAL_STATE) or {"pool": previous.get("pool", {})}
        self._blocked_until = 0

    def _read_shelf(self, accounts):
        books, usable = {}, set()
        for name, jar in accounts:
            try:
                result = json.loads(self.shelf_request(jar, _SHELF))
            except (OSError, TypeError, ValueError):
                continue
            if not isinstance(result, dict) or result.get("errCode") or not isinstance(result.get("books"), list):
                continue
            usable.add(name)
            for book in result["books"]:
                if isinstance(book, dict) and isinstance(info := book.get("bookInfo") or book, dict):
                    book_id = info.get("bookId")
                    if isinstance(book_id, str) and book_id.startswith("MP_WXS_"):
                        books[book_id] = {"bookId": book_id, "name": str(info.get("title") or "").strip()}
        return list(books.values()), usable

    def discover(self):
        try:
            accounts = self.pool.accounts()
            credentials = tuple((name, str(path), path.stat().st_mtime_ns) for name, path in accounts)
            if credentials != self._credentials:
                self._credentials = credentials
                self._shelf_due = 0
                self._blocked_until = 0
            if not accounts:
                self.sources, self._usable_accounts = (), set()
                return
            if time() < self._shelf_due:
                return
            self._shelf_due = time() + WEREAD_POLL_SECONDS
            shelf, self._usable_accounts = self._read_shelf(accounts)
            if not self._usable_accounts:
                self.sources = ()
                return
            registry = _read_json(self.official.REGISTRY, {"accounts": [], "include_unlisted": True})
            targets, _, _ = self.official.build_targets(registry, shelf, self._registry_state)
            cached = _read_json(self.official.POSTS, {}).get("accounts", {})
            self.index.add_wechat_accounts((target["account"] or {}).get("name") or target["name"] for target in targets)
            self.sources = tuple(self._source(target, cached.get(target["bookId"]) or {}) for target in targets)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as exc:
            _LOG.warning("校园日报无法更新公众号书架（%s）", type(exc).__name__)

    def _source(self, target, cached):
        book_id, name = target["bookId"], target["name"]
        registered = (target["account"] or {}).get("name", "")
        previous = [entry for raw in cached.get("posts", ())
                    if (entry := _article(raw, book_id, name, registered))]
        return Source(
            source_key("weread", book_id), partial(self._fetch, target),
            initial_anchors=tuple(resume_anchors(_distinct_articles(previous))),
            interval_seconds=WEREAD_POLL_SECONDS,
        )

    def ready_at(self):
        return max(self._blocked_until, self._shared.get("next_at", 0))

    def _fetch(self, target, cursor):
        now = time()
        if self.ready_at() > now:
            raise PollUnavailable(WEREAD_POLL_SECONDS, entire_source=True)
        accounts = self.pool.accounts()
        if not accounts or not self._usable_accounts:
            raise PollUnavailable(WEREAD_POLL_SECONDS, entire_source=True)
        # Reload account-level cooldowns which can also be updated by the report package.
        self.pool.state = self.pool._load_state()
        pool_state = self._shared.setdefault("pool", {})
        ready = min(max(pool_state.get(name, {}).get("until", 0),
                        self.pool.state.get(name, {}).get("cooldown_until", 0)) for name, _ in accounts)
        if ready > now:
            self._blocked_until = min(now + WEREAD_POLL_SECONDS, ready)
            raise PollUnavailable(WEREAD_POLL_SECONDS, entire_source=True)
        self._shared["next_at"] = now + WEREAD_POLL_SECONDS
        try:
            offset = cursor or 0
            status, payload, account = self.official.fetch_page(self._shared, target["bookId"], offset)
        finally:
            self.index.commit_poll(_GLOBAL_STATE, self._shared)
        if status == "limited":
            _LOG.info("微信读书文章列表已进入上游冷却")
            raise PollUnavailable(WEREAD_POLL_SECONDS, entire_source=True)
        if status == "expired":
            self._shelf_due = 0
            self._usable_accounts = self._usable_accounts - {account}
            raise PollUnavailable(WEREAD_POLL_SECONDS, entire_source=True)
        if status == "cooling":
            raise PollUnavailable(WEREAD_POLL_SECONDS, entire_source=True)
        if status != "ok" or not isinstance(payload, dict) or not isinstance(payload.get("reviews"), list):
            raise ValueError("微信读书文章列表读取失败")
        parsed = self.official.parse_page(payload)
        page = self._page(target, offset, payload["reviews"], parsed["has_more"])
        if payload["reviews"] and not page.entries:
            raise ValueError("微信读书推送返回了无法识别的文章")
        self.legacy_cache.accept(target, offset, parsed)
        return page

    def _page(self, target, offset, reviews, has_more):
        records = []
        book_id, name = target["bookId"], target["name"]
        registered = (target["account"] or {}).get("name", "")
        for review in reviews:
            group = review.get("reviewId") if isinstance(review, dict) else None
            alias = (f"weread://{book_id}/{group}",) if group else ()
            for review_id, created_at, info in self.official._walk(review):
                # Parse individually so two links with an identical title and time both survive.
                article = self.official.parse_page({
                    "reviews": [{"reviewId": review_id, "createTime": created_at, "mpInfo": info}],
                })["articles"][0]
                if entry := _article(article, book_id, name, registered, alias):
                    records.append(entry)
        consumed = len(reviews)
        more = consumed > 0 and has_more is not False
        if has_more is None and offset and consumed <= OVERLAP:
            more = False  # The upstream returned only the tail covered by the overlap.
        next_offset = offset + max(1, consumed - OVERLAP) if more else None
        overlap = tuple(
            f"weread://{book_id}/{group}"
            for review in reviews[-OVERLAP:]
            if isinstance(review, dict) and (group := review.get("reviewId"))
        )
        unique = {entry.key: entry for entry in _distinct_articles(records)}
        return Page(tuple(unique.values()), next_offset, overlap)
