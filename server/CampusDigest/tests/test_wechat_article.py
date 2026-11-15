import json
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from server.CampusDigest.wechat_article import ARTICLE_PAGE_CHARS, WeChatArticles, _download, _parse
from server.CampusDigest.index import DigestIndex
from test_index import DigestAPI


class ArticleReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.api = DigestAPI()
        self.source = "weread://MP_WXS_123/review-456"
        self.url = "https://mp.weixin.qq.com/s/official-article"
        self.jar = self.directory / "session.txt"
        self.accounts = [("fixture", self.jar)]
        self.article = {"title": "讲座安排", "time": "2026-11-14 16:00", "account": "中传学生会",
                        "link": self.source, "digest": "讲座摘要"}

    def reader(self, *articles):
        self.api.official = list(articles or (self.article,))
        index = DigestIndex(self.directory, self.directory / "digest.sqlite3", self.api)
        rows = index.read(query="", days=0, source="wechat")["items"]
        return WeChatArticles(index, lambda: self.accounts), [row["id"] for row in rows]

    def test_reads_the_public_body_and_paginates_the_same_copy(self):
        reader, (article_id,) = self.reader({**self.article, "link": self.url})
        details = "报告厅安排" * ARTICLE_PAGE_CHARS
        html = f'<body><header>页面菜单</header><div id="js_content"><section>{details}</section><script>隐藏脚本</script></div></body>'
        with patch("server.CampusDigest.wechat_article._download", return_value=("ok", html)) as download:
            first = reader.read(article_id, 0)
            second = reader.read(article_id, first["next_offset"])
        self.assertEqual((first["state"], first["content_source"], first["url"]), ("text", "public_article", self.url))
        self.assertEqual(first["content"] + second["content"], details[:2 * ARTICLE_PAGE_CHARS])
        download.assert_called_once_with(self.url)

    def test_download_keeps_large_original_pages(self):
        raw = ('<div id="js_content">原文内容</div><script>' + 'a' * (3 * 1024 * 1024) + '</script>').encode()
        with BytesIO(raw) as response, patch("server.CampusDigest.wechat_article.build_opener") as opener:
            response.headers = Message()
            response.headers["Content-Type"] = "text/html"
            opener.return_value.open.return_value = response
            state, page = _download(self.url)
        self.assertEqual(state, "ok")
        self.assertEqual(_parse(page), ("ok", "原文内容", None))

    def test_reads_id_only_article_using_the_existing_weread_account(self):
        reader, (article_id,) = self.reader()
        html = f'<link rel="canonical" href="{self.url}"><h1>讲座安排</h1><p>中传学生会</p><p>周五 19:30 在报告厅开讲。</p>'
        with patch("server.CampusDigest.wechat_article._download", return_value=("ok", html)) as download:
            article = reader.read(article_id, 0)
        download.assert_called_once_with("https://weread.qq.com/web/mp/content?bookId=MP_WXS_123&reviewId=review-456", self.jar)
        self.assertEqual((article["state"], article["content_source"], article["content"]),
                         ("text", "weread_article", "周五 19:30 在报告厅开讲。"))
        self.assertEqual((article["url"], article["published_on"]), (self.url, "2026-11-14"))
        self.assertNotIn("reviewId", repr(article))

    def test_reuses_only_matching_collected_text_and_reports_an_expired_login(self):
        cache = self.directory / "data" / "weread_items.json"
        cache.parent.mkdir()
        cache.write_text(json.dumps({"items": [{"url": self.source, "title": "讲座安排", "chars": 12,
                                                "excerpt": "讲座将在报告厅举行，现场签到。"}]}), encoding="utf-8")
        reader, (article_id,) = self.reader()
        with patch("server.CampusDigest.wechat_article._download", side_effect=AssertionError("already collected")):
            self.assertEqual(reader.read(article_id, 0)["content_source"], "collected_article")
        cache.write_text(json.dumps({"items": [{"url": self.source, "title": "另一条文章", "chars": 500,
                                                "excerpt": "不能混用的正文"}]}), encoding="utf-8")
        self.assertEqual(reader.read(article_id, 0)["content"], "讲座将在报告厅举行，现场签到。")
        fresh = DigestIndex(self.directory, self.directory / "fresh.sqlite3", self.api)
        fresh.refresh()
        reader = WeChatArticles(fresh, lambda: [("test", self.jar)])
        with patch("server.CampusDigest.wechat_article._download", return_value=("ok", '{"errCode":-2012}')):
            article = reader.read(article_id, 0)
        self.assertEqual((article["state"], article["issue"], article["content"]), ("excerpt", "login_required", "讲座摘要"))
        self.assertIsNone(reader.read("0" * 64, 0))
