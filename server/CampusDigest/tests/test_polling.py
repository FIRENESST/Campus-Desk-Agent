import html
import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from server.CampusDigest.collection import SourceCatalog
from server.CampusDigest.index import DigestIndex, _item
from server.CampusDigest.polling import POLL_SECONDS, Entry, Page, PollUnavailable, Source, SourcePoll, run_polling
from server.CampusDigest.weread_poll import WeReadPoll


def no_legacy(_query):
    return {"items": [], "total": 0}


def article(number, day="2026-11-14"):
    row = _item({"title": f"校园文章 {number}", "date": day, "account": "校园日报",
                 "url": f"https://mp.weixin.qq.com/s/post{number}"}, official=False)
    return Entry(row["id"], row)


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.db = self.root / "index.sqlite3"
        self.api = SimpleNamespace(get_items=no_legacy, get_official=no_legacy)
        self.index = DigestIndex(self.root, self.db, self.api)
        self.index.refresh()

    def test_restart_resumes_by_saved_page_even_when_an_article_was_backdated(self):
        pinned, old, middle, same_time = (article(number) for number in (0, 1, 2, 3))
        newest = article(4, "2026-11-10")
        timeline = {None: Page((pinned, old), 1)}
        seen = []

        def fetch(cursor):
            seen.append(cursor)
            page = timeline[cursor]
            if isinstance(page, Exception):
                raise page
            return page

        source = Source("account", fetch)
        poll = SourcePoll(self.index, source)
        poll.collect()
        self.assertNotIn("backfill", poll.progress)
        timeline.update({
            None: Page((pinned, newest, same_time), 2),
            2: Page((same_time, middle), 3),
            3: Page((middle, old), 4),
        })
        poll.collect()
        self.assertEqual(self.index.poll_state("account")["backfill"]["boundary"], [old.key])
        self.assertEqual(self.index.poll_state("account")["backfill"]["cursor"], 2)

        restarted = DigestIndex(self.root, self.db, self.api)
        poll = SourcePoll(restarted, source)
        page = timeline[2]
        timeline[2] = PollUnavailable(120)
        next_head = poll.due
        poll.run(backfill=True)
        self.assertEqual(poll.progress["backfill"]["cursor"], 2)
        self.assertEqual(poll.due, next_head)
        self.assertGreaterEqual(poll.backfill_due, poll.due)
        timeline[2] = page
        poll.catch_up()
        self.assertEqual(restarted.poll_state("account")["backfill"]["cursor"], 3)
        poll.collect()  # A normal head check must preserve the in-progress backfill.
        self.assertEqual(poll.progress["backfill"]["cursor"], 3)
        poll = SourcePoll(DigestIndex(self.root, self.db, self.api), source)
        poll.catch_up()
        self.assertIsNone(poll.progress["backfill"])
        self.assertEqual(seen, [None, None, 2, 2, None, 3])

        data = restarted.read(query="", source="wechat", days=0)
        self.assertEqual(len(data["items"]), 5)
        self.assertEqual(data["items"][-1]["id"], newest.key)
        self.assertEqual(data["items"][-1]["published_on"], "2026-11-10")
        poll.collect()
        self.assertEqual(restarted.read()["revision"]["version"], data["revision"]["version"])

    def test_shifted_offset_rechecks_head_before_trusting_a_boundary(self):
        old, middle, newest, inserted = (article(number) for number in (1, 2, 3, 4))
        pages = {
            None: Page((newest, middle), 1),
            1: Page((old,)),
        }
        poll = SourcePoll(self.index, Source("shifted", pages.__getitem__, initial_anchors=(old.key,)))
        poll.collect()
        poll.catch_up()
        self.assertEqual(poll.progress["backfill"]["cursor"], 0)
        pages[0] = Page((newest, inserted, middle), 2)
        pages[2] = Page((middle, old))
        poll.catch_up()
        poll.catch_up()
        self.assertIsNone(poll.progress["backfill"])
        self.assertIn(inserted.key, {row["id"] for row in self.index.read(query="", days=0)["items"]})

    def test_shared_request_slot_visits_each_due_source_before_repeating(self):
        stopping = Event()
        self.addCleanup(stopping.set)
        calls = []
        ticks = [0]
        next_request = [0]

        def fetch(source, _cursor):
            calls.append(source)
            next_request[0] = ticks[0] + 1
            if len(calls) == 6:
                stopping.set()
            return Page(())

        def ready_at():
            ticks[0] += 1
            return 0 if ticks[0] >= next_request[0] else float("inf")

        sources = [Source(name, lambda cursor, name=name: fetch(name, cursor)) for name in "abc"]
        with patch("server.CampusDigest.polling.time", side_effect=lambda: ticks[0] * POLL_SECONDS):
            run_polling(self.index, lambda: sources, stopping, workers=1, ready_at=ready_at)
        self.assertEqual(calls, list("abcabc"))

    def test_public_source_paths_are_independent_and_follow_pagination(self):
        base = "https://campus.example/main.htm"
        listing = "https://campus.example/news/list.htm"
        next_page = "https://campus.example/news/list2.htm"
        wx = "https://mp.weixin.qq.com/s/test-link"
        (self.root / "sources.json").write_text(json.dumps({
            "web_sources": [
                {"name": "教务处", "url": base, "extra_paths": ["/news/list.htm"]},
                {"name": "停用", "url": "https://disabled.example/", "enabled": False},
            ],
            "wechat_accounts": [{"name": "学生会", "enabled": True, "article_urls": [wx]}],
        }), encoding="utf-8")
        request_data = {
            base: "",
            listing: '<a rel="next" href="list2.htm">下一页</a>',
            next_page: '<a rel="next" href="https://elsewhere.example/news/list3.htm">下一页</a>',
            wx: '<script>var msg_title = "讲座通知"; var ct = "1794344420";</script>',
        }
        collect = SimpleNamespace(
            CONFIG=self.root, UA="website", UA_MOBILE_WX="wechat", unescape=html.unescape,
            strip_tags=lambda value: re.sub(r"<[^>]+>", "", value),
            crawl_list=lambda page, url: [{"title": "旧通知" if url == next_page else "新通知",
                                          "date": "2026-11-12", "url": url + "?article"}],
        )
        with patch("server.CampusDigest.collection._request", side_effect=lambda url, _: request_data[url]) as request:
            public = SourceCatalog(collect).public_sources()
            self.assertEqual(len({source.key for source in public}), 3)
            public[0].fetch(None)
            page = public[1].fetch(None)
            self.assertEqual(page.next_cursor, next_page)
            self.assertIsNone(public[1].fetch(page.next_cursor).next_cursor)
            (item,) = public[2].fetch(None).entries
            self.assertEqual(item.item["published_at"], "2026-11-11T05:00:20+08:00")
        self.assertEqual([call.args[0] for call in request.call_args_list], [base, listing, next_page, wx])

    def test_weread_uses_existing_cooldown_and_persists_a_new_one(self):
        target = {"bookId": "MP_WXS_book", "name": "公众号", "account": None}
        jar = self.root / "fixture.txt"
        jar.touch()
        registry = self.root / "registry.json"
        request_time = [100]
        status = [("limited", 45, "reader"), ("ok", {"reviews": []}, "reader")]
        requests = []

        def fetch(state, book, offset):
            requests.append((book, offset))
            current = status.pop(0)
            if current[0] == "limited":
                state["pool"]["reader"]["until"] = request_time[0] + 2700
            return current

        official = SimpleNamespace(
            STATE=self.root / "old-state.json", REGISTRY=registry, POSTS=self.root / "posts.json",
            load_json=lambda *_: {"pool": {"reader": {"until": 150}}},
            build_targets=lambda *_: ([target], [], {}),
            fetch_page=fetch, parse_page=lambda _: {"articles": [], "has_more": False},
        )
        pool = SimpleNamespace(accounts=lambda: [("reader", jar)], _load_state=lambda: {})
        cache = SimpleNamespace(accept=lambda *_: None)
        shelf = lambda *_: json.dumps({"books": [{"bookId": target["bookId"], "title": "公众号"}]})
        with patch("server.CampusDigest.weread_poll.time", side_effect=lambda: request_time[0]):
            collector = WeReadPoll(self.index, official, pool, shelf, cache)
            collector.discover()
            (source,) = collector.sources
            with self.assertRaises(PollUnavailable):
                source.fetch(None)
            self.assertEqual(collector.ready_at(), 150)
            self.assertEqual(requests, [])
            request_time[0] = 150
            with self.assertRaises(PollUnavailable):
                source.fetch(None)
            self.assertEqual(requests, [(target["bookId"], 0)])
            self.assertEqual(self.index.poll_state("weread:shared")["pool"]["reader"]["until"], 2850)
            request_time[0] = 160
            restarted = WeReadPoll(self.index, official, pool, shelf, cache)
            restarted.discover()
            with self.assertRaises(PollUnavailable):
                restarted.sources[0].fetch(18)
            request_time[0] = 2850
            self.assertEqual(restarted.sources[0].fetch(18), Page(()))
            self.assertEqual(requests[-1], (target["bookId"], 18))
            self.assertEqual(restarted.ready_at(), 3150)
            request_time[0] = 3149
            with self.assertRaises(PollUnavailable):
                restarted.sources[0].fetch(None)
            self.assertEqual(len(requests), 2)
