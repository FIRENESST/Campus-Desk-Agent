import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from urllib.parse import urlencode

from server.CampusDigest.index import MAX_ITEMS, DigestIndex, _item
from server.CampusDigest.managed import _filters, _handler, _jobs, _runner, _wechat_filters


class DigestAPI:
    def __init__(self):
        self.calls = 0
        self.items = [{
            "title": "校园讲座通知", "date": "2026-11-10", "kind": "官网", "account": "教务处",
            "url": "https://campus.example.test/lecture", "excerpt": "线下报名说明",
            "insight": {"截止": "11 月 13 日 18:00"}, "links": ["https://private.example.test/signup"],
        }]
        self.official = [{
            "title": "志愿活动招募", "time": "2026-11-11 08:00", "official_name": "学生会",
            "account": "学生会 ＣＵＣ", "link": "weread://account/article", "digest": "志愿者报名开放",
        }]

    def _page(self, rows, params):
        self.calls += 1
        offset, limit = int(params["offset"][0]), int(params["limit"][0])
        return {"total": len(rows), "items": rows[offset:offset + limit]}

    def get_items(self, params):
        return self._page(self.items, params)

    def get_official(self, params):
        return self._page(self.official, params)


class DigestIndexTests(unittest.TestCase):
    def test_tool_search_ranks_title_before_metadata_and_body_matches(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        api = DigestAPI()
        api.items = [{
            "title": "新生报到须知", "date": "2026-11-10", "kind": "官网",
            "url": "https://campus.example.test/registration",
        }, {
            "title": "学院周报", "date": "2026-11-11", "kind": "官网", "excerpt": "回顾新生报到",
        }]
        api.official = [{
            "title": "新生报到通知", "time": "2026-11-10 08:00", "official_name": "学生会",
            "link": "https://mp.weixin.qq.com/s/registration",
        }, {
            "title": "学生会周报", "time": "2026-11-12 08:00", "official_name": "学生会",
        }]

        class TestHandlerBase:
            def _json(self, status, data):
                self.status = status
                return data

        path = directory / "digest.sqlite3"
        index = DigestIndex(directory, path, api)
        feed = index.read(**_wechat_filters("account=学生会"))
        report, announcement = feed["items"]
        index.save_article_body(report["id"], "请在新生报到时携带报名凭证。", "collected_article", None)

        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DROP INDEX digest_item_by_source_date")
            db.execute("DROP INDEX digest_wechat_by_publisher")
        index = DigestIndex(directory, path, api)
        handler = _handler(TestHandlerBase, index, None, "", None)()
        handler.path = "/api/campus-desk/search?" + urlencode({"source": "web", "q": "新生 报到", "days": 0, "limit": 1})
        website_search = handler.do_GET()
        self.assertEqual(handler.status, 200)
        self.assertEqual(website_search["items"][0]["url"], "https://campus.example.test/registration")
        self.assertTrue(website_search["has_more"])

        handler.path = "/api/campus-desk/search?" + urlencode({"source": "wechat", "q": "新生 报到", "days": 0})
        wechat_search = handler.do_GET()
        self.assertEqual(handler.status, 200)
        self.assertEqual([row["id"] for row in wechat_search["items"]], [announcement["id"], report["id"]])
        self.assertIn("报名凭证", wechat_search["items"][1]["body_excerpt"])
        self.assertEqual(wechat_search["body_coverage"], {"indexed": 1, "total": 2})
        accounts = index.wechat_accounts(query="学生会")
        self.assertEqual(accounts["accounts"], [{"id": report["account_id"], "name": "学生会"}])
        new_feed = index.read(**_wechat_filters("account=学生会"))
        self.assertEqual([row["id"] for row in new_feed["items"]], [report["id"], announcement["id"]])
        self.assertEqual(new_feed["revision"], feed["revision"])

    def test_wechat_accounts_followed_order_and_article_body_search(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        (directory / "config").mkdir()
        (directory / "data").mkdir()
        (directory / "config" / "official_accounts.json").write_text(json.dumps({"accounts": [
            {"name": "仅登记的公众号"}, {"name": "学生会"},
        ]}, ensure_ascii=False), encoding="utf-8")
        original = "https://mp.weixin.qq.com/s/example-lecture"
        (directory / "data" / "mp_articles.json").write_text(json.dumps([{
            "title": "志愿活动招募", "account": "学生会", "date": "2026-11-11", "url": original,
            "body": "请携带选课系统生成的报名凭证。",
        }], ensure_ascii=False), encoding="utf-8")
        api = DigestAPI()
        api.official[0]["link"] = original
        api.official.append({"title": "新文章", "account": "另一公众号", "time": "2026-11-12 11:30"})
        index = DigestIndex(directory, directory / "index.sqlite3", api)
        self.assertEqual({row["name"] for row in index.wechat_accounts()["accounts"]}, {"学生会", "另一公众号", "仅登记的公众号"})
        (selected,) = index.wechat_accounts(query="学生会")["accounts"]

        defaults = {"query": "", "source": "wechat", "days": 0, "limit": 1}
        self.assertEqual(index.read(**defaults)["items"][0]["title"], "新文章")
        pinned = index.read(**defaults, followed=[selected["id"]])
        self.assertTrue(pinned["has_more"])
        self.assertEqual(pinned["items"][0]["title"], "志愿活动招募")
        self.assertTrue(pinned["items"][0]["followed"])
        self.assertEqual(index.read(**defaults, followed=[selected["id"]], offset=1)["items"][0]["title"], "新文章")

        result = index.read(**defaults, body="选课系统 报名凭证")
        self.assertEqual(result["body_coverage"], {"indexed": 1, "total": 2})
        self.assertIn("选课系统", result["items"][0]["body_excerpt"])
        self.assertEqual(index.read(**{**defaults, "query": "报名凭证"})["items"][0]["title"], "志愿活动招募")
        self.assertFalse(index.read(**defaults, body="志愿活动招募")["items"])

    def test_legacy_schedule_keeps_daily_and_login_renewal(self):
        daily = SimpleNamespace(name="daily", every_min=0, cmd=["daily"])
        official = SimpleNamespace(name="official", every_min=20, cmd=["official", "collect"])
        slow = SimpleNamespace(name="weread_slow", every_min=15, cmd=["slow"])
        renew = SimpleNamespace(name="weread_renew", every_min=30, cmd=["renew"])
        self.assertEqual(_jobs(SimpleNamespace(build_jobs=lambda: [daily, official, slow, renew])), [daily, renew])

    def test_global_retention_orders_by_publication_and_keeps_independent_checkpoints(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        api = DigestAPI()
        api.items, api.official = [], []
        path = directory / "digest.sqlite3"
        index = DigestIndex(directory, path, api)
        index.refresh()
        first_day = datetime(2026, 10, 1, tzinfo=timezone.utc)
        items = []
        for number in range(MAX_ITEMS + 2):
            published = first_day + timedelta(minutes=number)
            items.append(_item({
                "title": f"文章 {number}", "account": "校园新闻",
                "url": f"https://campus.example/posts/{number}", "ts": int(published.timestamp()),
            }, official=False))
        index.commit_poll("old-source", {"anchors": [items[0]["id"]]}, items[:1])
        index.commit_poll("other-source", {"anchors": [items[-1]["id"]]}, reversed(items[1:]))
        index = DigestIndex(directory, path, api)
        data = index.read(query="", days=0, limit=MAX_ITEMS)
        self.assertEqual(MAX_ITEMS, data["revision"]["item_count"])
        self.assertEqual([item["id"] for item in data["items"]], [item["id"] for item in reversed(items[2:])])
        self.assertEqual(index.poll_state("old-source")["anchors"], [items[0]["id"]])
        index.commit_poll("old-source", {"anchors": [items[0]["id"]]}, items[:1])
        self.assertEqual(index.read()["revision"]["version"], data["revision"]["version"])

    def test_wechat_uses_distinct_article_ids_for_same_title_and_timestamp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        api = DigestAPI()
        api.items, api.official = [], []
        index = DigestIndex(directory, directory / "digest.sqlite3", api)
        articles = [
            _item({
                "title": "同名文章", "ts": 1794344420, "account": "学生会", "source_id": "weread://account/group",
                "link": f"https://mp.weixin.qq.com/s?__biz=abc&mid=12&idx={number}&scene=1",
            }, official=True)
            for number in (1, 2)
        ]
        tracked = _item({
            "title": "同名文章", "ts": 1794344420, "account": "学生会",
            "link": "https://mp.weixin.qq.com/s?idx=1&scene=2&mid=12&__biz=abc#rd",
        }, official=True)
        index.commit_poll("weread", {}, articles)
        index.commit_poll("link", {}, [tracked])
        data = index.read(query="", days=0, source="wechat")
        self.assertEqual({item["id"] for item in data["items"]}, {item["id"] for item in articles})
        self.assertEqual({item["published_at"] for item in data["items"]}, {articles[0]["published_at"]})

    def test_publication_advances_the_persistent_revision_once_and_keeps_last_good_data(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        source = directory / "data" / "items.json"
        source.parent.mkdir()
        source.write_text('{"items": []}', encoding="utf-8")
        api = DigestAPI()
        path = directory / "digest.sqlite3"
        index = DigestIndex(directory, path, api)
        first = index.read(query="讲座 报名", days=0)
        item, = first["items"]
        self.assertEqual(first["revision"]["version"], 1)
        self.assertEqual((item["publisher"], item["url"], item["registration_deadline"]),
                         ("教务处", "https://campus.example.test/lecture", "11 月 13 日 18:00"))
        self.assertNotIn("private.example.test", json.dumps(first))
        calls = api.calls
        self.assertEqual(index.read(query="招募", days=0, source="wechat")["items"][0]["source_id"],
                         "weread://account/article")
        self.assertEqual(DigestIndex(directory, path, api).read()["revision"]["version"], 1)
        self.assertEqual(api.calls, calls)

        class Runner:
            def __init__(self):
                self.stopping = Event()

            def run(self, job, extra_env=None):
                api.items.append({"title": "新的校园讲座", "date": "2026-11-12", "kind": "官网"})
                source.write_text('{"items": [], "updated_at": "new"}', encoding="utf-8")
                return 0

        runner = _runner(SimpleNamespace(Runner=Runner), index)
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiting = executor.submit(index.wait_for_revision, 1, 5)
            self.assertEqual(runner.run(SimpleNamespace(name="daily")), 0)
            self.assertEqual(waiting.result(timeout=5), 2)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT version,item_count FROM digest_revision").fetchone(), (2, 3))
        source.write_text('{"items": [], "updated_at": "same articles"}', encoding="utf-8")
        self.assertEqual(index.read()["revision"]["version"], 2)

        source.write_text('{"items":', encoding="utf-8")
        with self.assertLogs("server.CampusDigest.index", level="WARNING"):
            fallback = index.read(query="讲座", days=0)
        self.assertTrue(fallback["stale"])
        self.assertEqual(fallback["revision"]["version"], 2)
        self.assertEqual([row["title"] for row in fallback["items"]], ["新的校园讲座", "校园讲座通知"])

    def test_wechat_filter_uses_names_and_rebuilds_an_existing_index(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        api = DigestAPI()
        api.items.append({"title": "志愿活动招募", "date": "2026-11-11", "kind": "公众号", "account": "学生会 ＣＵＣ",
                          "url": "https://mp.weixin.qq.com/s/example"})
        path = directory / "digest.sqlite3"
        index = DigestIndex(directory, path, api)
        first = index.read(query="", days=0, source="wechat")
        self.assertEqual(len(first["items"]), 1)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("ALTER TABLE digest_item DROP COLUMN publisher_search")
            db.execute("UPDATE digest_revision SET content_hash='old schema' WHERE singleton=1")
        index = DigestIndex(directory, path, api)
        for account in ("学生会", "cuc", "学生会  CUC"):
            with self.subTest(account=account):
                data = index.read(**_wechat_filters("account=" + account))
                self.assertEqual(data["revision"]["version"], 2)
                self.assertEqual([item["url"] for item in data["items"]], ["https://mp.weixin.qq.com/s/example"])
        self.assertFalse(index.read(**_wechat_filters("account=报名"))["items"])
        self.assertFalse(index.read(**_wechat_filters("offset=12"))["items"])

    def test_wechat_search_matches_titles_before_pagination_and_recovers_publication_times(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        source = directory / "data" / "weread_items.json"
        source.parent.mkdir()
        timestamp = int(datetime(2026, 11, 10, 16, 23, 45, tzinfo=timezone.utc).timestamp())
        url = "weread://account/exact-time"
        source.write_text(json.dumps({"items": [{"url": url, "ts": timestamp}]}), encoding="utf-8")
        api = DigestAPI()
        api.items.extend([
            {"title": "校园讲座通知", "date": "2026-11-12", "kind": "公众号", "account": "学生会", "excerpt": "志愿活动"},
            {"title": "ＣＵＣ 志愿活动 招募", "date": "2026-11-11", "kind": "公众号", "account": "学生会", "url": url},
            {"title": "志愿活动招募", "date": "2026-11-10", "kind": "公众号", "account": "青年协会"},
        ])
        path = directory / "digest.sqlite3"
        index = DigestIndex(directory, path, api)
        first = index.read(**_wechat_filters(urlencode({"title": "cuc 招募", "account": "学生会", "offset": 0})))
        item, = first["items"]
        self.assertEqual(item["title"], "ＣＵＣ 志愿活动 招募")
        self.assertEqual((item["published_on"], item["published_at"]), ("2026-11-11", "2026-11-11T00:23:45+08:00"))

        filters = {**_wechat_filters("title=志愿活动&account=学生会"), "limit": 1}
        page = index.read(**filters)
        self.assertEqual([row["title"] for row in page["items"]], ["志愿活动招募"])
        self.assertTrue(page["has_more"])
        self.assertEqual(page["items"][0]["published_at"], "2026-11-11T08:00:00+08:00")
        next_page = index.read(**{**filters, "offset": 1})
        self.assertEqual([row["title"] for row in next_page["items"]], [item["title"]])
        self.assertFalse(next_page["has_more"])
        date_only, = index.read(**_wechat_filters("title=志愿活动&account=青年协会"))["items"]
        self.assertEqual(date_only["published_on"], "2026-11-10")
        self.assertIsNone(date_only["published_at"])

        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE digest_revision SET source_stamp='legacy',content_hash='legacy'")
            db.execute("UPDATE digest_item SET published_at=NULL")
        rebuilt = DigestIndex(directory, path, api).read(**filters)
        self.assertEqual(rebuilt["revision"]["version"], first["revision"]["version"] + 1)
        self.assertEqual(rebuilt["items"][0]["published_at"], "2026-11-11T08:00:00+08:00")

    def test_search_parameters_are_bounded(self):
        self.assertEqual(_filters("source=wechat&days=0&limit=8"),
                         {"query": "", "source": "wechat", "days": 0, "limit": 8})
        for query in ("q=a&q=b", "limit=9", "days=-1", "path=external", "source=unknown"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                _filters(query)
        for query in ("account=a&account=b", "title=a&title=b", "title=" + "a" * 201,
                      "offset=-1", "offset=1000000", "source=web"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                _wechat_filters(query)

    def test_wechat_link_lookup_uses_the_same_article_ids_as_the_list(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        api = DigestAPI()
        original = "https://mp.weixin.qq.com/s?__biz=abc&mid=12&idx=1&sn=keep&scene=1"
        api.official = [{"title": "社团招新", "account": "学生会", "link": original}]
        index = DigestIndex(directory, directory / "digest.sqlite3", api)
        listing = index.read(**_wechat_filters("account=学生会&title=招新"))
        filters = _filters(urlencode({"source": "wechat", "days": 0, "url": original.replace("scene=1", "scene=2")}))
        self.assertEqual(index.read(**filters)["items"], listing["items"])
        article, = listing["items"]
        self.assertEqual(index.wechat_article(article["id"])["title"], "社团招新")
        web, = index.read(query="", days=0, source="web")["items"]
        self.assertIsNone(index.wechat_article(web["id"]))
        for url in ("https://mp.weixin.qq.com.example.test/s/abc", "http://mp.weixin.qq.com/s/abc",
                    "https://mp.weixin.qq.com/anything", "https://username@mp.weixin.qq.com/s/abc"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _filters(urlencode({"source": "wechat", "url": url}))
