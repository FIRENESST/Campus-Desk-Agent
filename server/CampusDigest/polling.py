"""Poll source lists independently and checkpoint each successful page."""

import logging
import sqlite3
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from hashlib import sha256
from time import time
from typing import Callable


POLL_SECONDS = 5
ANCHOR_COUNT = 8
OVERLAP = 5
_LOG = logging.getLogger(__name__)


class PollUnavailable(Exception):
    def __init__(self, retry_after, *, entire_source=False):
        self.retry_after = max(POLL_SECONDS, retry_after)
        self.entire_source = entire_source


@dataclass(frozen=True)
class Entry:
    key: str
    item: dict
    aliases: tuple[str, ...] = ()

    def identifiers(self):
        return (self.key, *self.aliases)


@dataclass(frozen=True)
class Page:
    entries: tuple[Entry, ...]
    next_cursor: int | str | None = None
    overlap_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class Source:
    key: str
    fetch: Callable[[int | str | None], Page]
    initial_anchors: tuple[str, ...] = ()
    interval_seconds: int = POLL_SECONDS


def _anchor_keys(entries):
    return list(dict.fromkeys(key for entry in entries[:ANCHOR_COUNT] for key in entry.identifiers()))


def _observed(entries):
    # Match push groups even if one group contains multiple articles.
    return {key for entry in entries for key in entry.identifiers()}


def resume_anchors(entries):
    if not entries:
        return []
    first = set(entries[0].identifiers())
    # A pinned first article alone cannot prove that the two lists overlap below it.
    below_first = [entry for entry in entries[1:] if first.isdisjoint(entry.identifiers())]
    return _anchor_keys(below_first or entries)


def _backfill(boundary, page, *, checked=(), restarted=False):
    return {
        "boundary": boundary, "cursor": page.next_cursor,
        "previous": list(page.overlap_keys) or _anchor_keys(page.entries[-ANCHOR_COUNT:]),
        "checked": list(checked[-8:]), "restarted": restarted,
    }


@dataclass
class SourcePoll:
    index: object
    source: Source
    progress: dict = field(init=False)

    def __post_init__(self):
        self.progress = self.index.poll_state(self.source.key)

    @property
    def due(self):
        return self.progress.get("next_at", 0)

    @property
    def backfill_due(self):
        if self.progress.get("backfill"):
            return self.progress.get("backfill_at", 0)
        return float("inf")

    def collect(self):
        started = time()
        progress = dict(self.progress)
        page = self.source.fetch(None)
        entries = page.entries
        pending = progress.get("backfill")
        if entries:
            anchors = progress.get("anchors", list(self.source.initial_anchors))
            previous = pending["boundary"] if pending else anchors
            observed = _observed(entries)
            if set(previous) & observed or page.next_cursor is None:
                pending = None
            elif previous and (pending is None or not (set(anchors) & observed)):
                pending = _backfill(previous, page)
            progress["anchors"] = resume_anchors(entries)
            if pending != progress.get("backfill"):
                progress.update(backfill=pending, backfill_at=time())
                progress.pop("backfill_failures", None)
        progress.pop("failure_count", None)
        progress["next_at"] = started + self.source.interval_seconds
        self._commit(progress, entries)

    def catch_up(self):
        started = time()
        pending = self.progress["backfill"]
        cursor = pending["cursor"]
        page = self.source.fetch(cursor)
        progress = dict(self.progress)
        boundary = pending["boundary"]
        observed = _observed(page.entries)
        checked = [*pending["checked"], cursor] if isinstance(cursor, str) else []
        if isinstance(cursor, int) and cursor and not pending["restarted"] and not (set(pending["previous"]) & observed):
            # Offset cursors drift when a source deletes or reorders articles.
            progress["backfill"] = {**pending, "cursor": 0, "checked": [], "restarted": True}
        elif (set(boundary) & observed or page.next_cursor is None
              or page.next_cursor == cursor or page.next_cursor in checked):
            progress["backfill"] = None
        else:
            progress["backfill"] = _backfill(boundary, page, checked=checked, restarted=pending["restarted"])
        progress.pop("backfill_failures", None)
        progress["backfill_at"] = started + self.source.interval_seconds
        self._commit(progress, page.entries)

    def run(self, *, backfill=False):
        try:
            if backfill:
                self.catch_up()
            else:
                self.collect()
        except PollUnavailable as exc:
            until = time() + exc.retry_after
            if exc.entire_source:
                self._commit({**self.progress, "next_at": until, "backfill_at": until})
            else:
                self._defer(until, backfill)
        except (OSError, TypeError, ValueError, KeyError, AttributeError) as exc:
            failure_key = "backfill_failures" if backfill else "failure_count"
            failures = min(self.progress.get(failure_key, 0) + 1, 6)
            until = time() + self.source.interval_seconds * 2 ** failures
            self._defer(until, backfill, **{failure_key: failures})
            _LOG.warning("校园日报来源采集失败（%s）", type(exc).__name__)

    def _defer(self, until, backfill, **details):
        key = "backfill_at" if backfill else "next_at"
        self._commit({**self.progress, **details, key: until})

    def _commit(self, progress, entries=()):
        self.index.commit_poll(self.source.key, progress, tuple(entry.item for entry in entries))
        self.progress = progress


def source_key(kind, identifier):
    return kind + ":" + sha256(identifier.encode()).hexdigest()


def run_polling(index, sources, stopping, *, workers, ready_at=None):
    polls, active = {}, {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="digest-poll") as pool:
        while not stopping.is_set():
            try:
                configured = {source.key: source for source in sources()}
                for key, source in configured.items():
                    if key not in polls:
                        polls[key] = SourcePoll(index, source)
                    elif key not in active:
                        polls[key].source = source
                for key in polls.keys() - configured.keys() - active.keys():
                    del polls[key]
            except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, AttributeError) as exc:
                _LOG.warning("校园日报读取来源失败（%s）", type(exc).__name__)
                stopping.wait(POLL_SECONDS)
                continue
            for key, future in list(active.items()):
                if future.done():
                    del active[key]
                    try:
                        future.result()
                    except (sqlite3.Error, OSError) as exc:
                        _LOG.warning("校园日报未能保存续爬进度（%s）", type(exc).__name__)
                        stopping.wait(POLL_SECONDS)
            now = time()
            idle = [poll for key, poll in polls.items() if key in configured and key not in active]
            idle.sort(key=lambda poll: min(poll.due, poll.backfill_due))
            if idle and len(active) < workers and (ready_at is None or now >= ready_at()):
                for poll in idle[:workers - len(active)]:
                    if min(poll.due, poll.backfill_due) > now:
                        break
                    active[poll.source.key] = pool.submit(poll.run, backfill=poll.backfill_due < poll.due)
            if active:
                wait(active.values(), timeout=0.5, return_when=FIRST_COMPLETED)
            else:
                stopping.wait(0.5)
