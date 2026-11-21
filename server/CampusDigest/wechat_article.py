"""Read text from an indexed WeChat article via its original page or WeRead."""

import json
import re
from collections import OrderedDict
from http.cookiejar import MozillaCookieJar
from html.parser import HTMLParser
from threading import Lock
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .collection import _read_json
from .index import (
    ARTICLE_CHALLENGE, MAX_ARTICLE_TEXT, UNAVAILABLE_ARTICLE, _normalize, _reference, _text, _url_key, is_article_placeholder,
)


ARTICLE_PAGE_CHARS = 4800
ARTICLE_MAX_CHARS = MAX_ARTICLE_TEXT
_MAX_HTML_BYTES = 8 * 1024 * 1024
_MAX_CACHE_ITEMS = 64
_WECHAT_ID = re.compile(r"weread://(MP_WXS_[A-Za-z0-9_-]{1,100})/([A-Za-z0-9_-]{1,160})")
_HTML_TYPES = {"text/html", "application/xhtml+xml", "application/json", "text/plain"}
_READER_CHROME = {"原创", "分享", "收藏", "在看", "留言", "赞赏", "关注该公众号", "去阅读", "在小说阅读器读本章"}
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def public_article_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(char.isspace() or char == "\\" for char in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.netloc.lower() not in {"mp.weixin.qq.com", "mp.weixin.qq.com:443"}:
            return None
        if re.fullmatch(r"/s/[A-Za-z0-9_-]+/?", parts.path):
            return f"https://mp.weixin.qq.com{parts.path.rstrip('/')}"
        fields = parse_qs(parts.query, max_num_fields=40)
        required = ("__biz", "mid", "idx")
        if parts.path == "/s" and all(len(fields.get(name, ())) == 1 for name in required):
            keep = (*required, "sn") if len(fields.get("sn", ())) == 1 else required
            return "https://mp.weixin.qq.com/s?" + urlencode([(key, fields[key][0]) for key in keep])
    except ValueError:
        return None
    return None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download(url, jar_path=None):
    handlers = [ProxyHandler({}), _NoRedirect()]
    headers = {"User-Agent": _UA, "Accept": "text/html, application/json", "Accept-Encoding": "identity"}
    deadline = monotonic() + 8
    try:
        if jar_path is not None:
            jar = MozillaCookieJar(str(jar_path))
            jar.load(ignore_discard=True)
            if not any(not cookie.is_expired() for cookie in jar):
                return "login_required", ""
            handlers.append(HTTPCookieProcessor(jar))
            headers["Referer"] = "https://weread.qq.com/"
        with build_opener(*handlers).open(Request(url, headers=headers), timeout=4) as response:
            if response.headers.get_content_type() not in _HTML_TYPES:
                return "unavailable", ""
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                return "unavailable", ""
            body = bytearray()
            while monotonic() < deadline and len(body) <= _MAX_HTML_BYTES:
                block = response.read1(min(64 * 1024, _MAX_HTML_BYTES + 1 - len(body)))
                if not block:
                    return "ok", body.decode("utf-8-sig", errors="replace")
                body.extend(block)
    except HTTPError as error:
        error.close()
        if error.code in {401, 403} and jar_path is not None:
            return "login_required", ""
        if error.code in {429, 503}:
            return "temporarily_unavailable", ""
    except (OSError, ValueError):
        pass
    return "unavailable", ""


class _ArticleText(HTMLParser):
    _BLOCKS = {"p", "div", "section", "h1", "h2", "h3", "h4", "li", "br", "tr", "blockquote"}
    _HIDDEN = {"script", "style", "svg", "template", "noscript", "head"}
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, *, fallback):
        super().__init__()
        self._fallback = fallback
        self._stack = []
        self._parts = []
        self._whole_page = []
        self._hidden = 0
        self._body = 0
        self.url = None

    def _write(self, text):
        if not self._hidden:
            if self._body:
                self._parts.append(text)
            if self._fallback:
                self._whole_page.append(text)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "link" and attributes.get("rel") == "canonical":
            self.url = public_article_url(attributes.get("href")) or self.url
        elif tag == "meta" and attributes.get("property") == "og:url":
            self.url = public_article_url(attributes.get("content")) or self.url
        body = attributes.get("id") == "js_content" or "rich_media_content" in (attributes.get("class") or "").split()
        hidden = tag in self._HIDDEN
        if tag not in self._VOID:
            self._stack.append((tag, body, hidden))
            self._body += body
            self._hidden += hidden
        if tag in self._BLOCKS:
            self._write("\n")

    def handle_endtag(self, tag):
        if tag in self._BLOCKS:
            self._write("\n")
        for position in range(len(self._stack) - 1, -1, -1):
            if self._stack[position][0] == tag:
                for _, body, hidden in self._stack[position:]:
                    self._body -= body
                    self._hidden -= hidden
                del self._stack[position:]
                break

    def handle_data(self, data):
        self._write(re.sub(r"\s+", " ", data))

    def text(self):
        parts = self._parts or self._whole_page
        return "\n".join(line for section in "".join(parts).splitlines() if (line := " ".join(section.split())))


def _parse(raw, *, from_weread=False):
    if raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            return "unavailable", "", None
        if not isinstance(data, dict):
            return "unavailable", "", None
        if data.get("errCode") in (-2010, -2012):
            return "login_required", "", None
        if data.get("errCode") == -2041:
            return "temporarily_unavailable", "", None
        if not from_weread or data.get("errCode"):
            return "unavailable", "", None
        fields = data.get("data") if isinstance(data.get("data"), dict) else data
        raw = next((fields[key] for key in ("content", "html", "articleContent") if isinstance(fields.get(key), str)), "")
    parser = _ArticleText(fallback=from_weread)
    try:
        parser.feed(raw)
        parser.close()
    except (ValueError, AssertionError):
        return "unavailable", "", None
    text = parser.text().replace("\x00", "")
    if len(text) < 400 and ARTICLE_CHALLENGE.search(text or raw[:4000]):
        return "temporarily_unavailable", "", None
    if not text or text.strip(" 。！") in UNAVAILABLE_ARTICLE:
        return "unavailable", "", None
    return "ok", text, parser.url


def _weread_body(text, article):
    title, account = article["title"], article["publisher"]
    lines = [line for line in text.splitlines() if line not in _READER_CHROME]
    headings = {title, account, f"{title} {account}", f"{title} · {account}"}
    start = 0
    while start < len(lines) and lines[start] in headings:
        start += 1
    return "\n".join(lines[start:])


def _collected_text(directory, article):
    try:
        document = _read_json(directory / "data" / "weread_items.json", {})
    except (OSError, ValueError, RecursionError):
        return "", False
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        return "", False
    title = _normalize(article["title"])
    complete_text, excerpts = "", ""
    for row in document["items"]:
        if not isinstance(row, dict) or _normalize(_text(row.get("title"), 240)) != title:
            continue
        url, source = _reference(row.get("url"))
        same_url = url and article["url"] and _url_key(url) == _url_key(article["url"])
        same_source = source and source == article["source_id"] and (
            not article["shared_source"] or row.get("date") == article["published_on"]
        )
        if not (same_url or same_source):
            continue
        saved = row.get("excerpt")
        if not isinstance(saved, str) or is_article_placeholder(saved, article["title"], article["publisher"]):
            continue
        count = row.get("chars")
        entire = type(count) is int and 0 < count <= len(saved)
        if entire and len(saved) > len(complete_text):
            complete_text = saved.strip()
        elif len(saved) > len(excerpts):
            excerpts = saved.strip()
    return complete_text or excerpts, bool(complete_text)


class WeChatArticles:
    def __init__(self, index, accounts):
        self.index, self.accounts = index, accounts
        self._read_slot = Lock()
        self._cache_lock = Lock()
        self._cache = OrderedDict()
        self._weread_wait = 0
        self._account_cursor = 0
        self._public_wait = 0
        self._public_retries = {}

    def _read_original(self, article, url):
        issue = "unavailable"
        if url:
            issue, raw = _download(url)
            if raw:
                issue, text, canonical = _parse(raw)
                if text:
                    return "public_article", text, canonical or url, None
        weread = _WECHAT_ID.fullmatch(article["source_id"] or "")
        if weread is None or article["shared_source"]:
            return "", "", url, issue
        if monotonic() < self._weread_wait:
            return "", "", url, "temporarily_unavailable"
        accounts = self.accounts()
        if not accounts:
            return "", "", url, "login_required"
        _, jar = accounts[self._account_cursor % len(accounts)]
        path = "https://weread.qq.com/web/mp/content?" + urlencode(dict(zip(("bookId", "reviewId"), weread.groups())))
        self._weread_wait = monotonic() + 3
        issue, raw = _download(path, jar)
        if raw:
            issue, text, canonical = _parse(raw, from_weread=True)
            text = _weread_body(text, article)
            if not is_article_placeholder(text, article["title"], article["publisher"]):
                return "weread_article", text, canonical or url, None
            if issue == "ok":
                issue = "unavailable"
        if issue == "temporarily_unavailable":
            self._weread_wait = monotonic() + 60
        elif issue == "login_required":
            self._account_cursor += 1
        return "", "", url, issue

    def read(self, article_id, offset):
        article = self.index.wechat_article(article_id)
        if article is None:
            return None
        url = public_article_url(article["url"])
        key = (article_id, url, article["source_id"], article["title"], article["shared_source"], article["published_at"])
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached and cached[0] > monotonic():
                self._cache.move_to_end(key)
                return self._page(article, offset, *cached[1])
        if saved := self.index.article_body(article_id):
            return self._page(article, offset, saved["content_source"], saved["content"], saved["url"] or url, None)
        if self.index.read_only:
            return self._page(article, offset, "indexed_excerpt", article["excerpt"], url, "unavailable")
        collected, complete = _collected_text(self.index.directory, article)
        if complete:
            self.index.save_article_body(article_id, collected, "collected_article", url)
            return self._page(article, offset, "collected_article", collected, url, None)

        if not self._read_slot.acquire(blocking=False):
            return self._page(article, offset, "indexed_excerpt", collected or article["excerpt"], url, "busy")
        try:
            source, text, url, issue = self._read_original(article, url)
            if not text:
                return self._page(article, offset, "indexed_excerpt", collected or article["excerpt"], url, issue)
            self.index.save_article_body(article_id, text, source, url)
            result = (source, text[:ARTICLE_MAX_CHARS + 1], url, issue)
            with self._cache_lock:
                self._cache[key] = (monotonic() + 300, result)
                self._cache.move_to_end(key)
                if len(self._cache) > _MAX_CACHE_ITEMS:
                    self._cache.popitem(last=False)
            return self._page(article, offset, *result)
        finally:
            self._read_slot.release()

    def prefetch_public(self):
        """Index one available public article page without spending a WeRead request."""
        now = monotonic()
        if now < self._public_wait or not self._read_slot.acquire(blocking=False):
            return
        try:
            self._public_retries = {key: cutoff for key, cutoff in self._public_retries.items() if cutoff > now}
            for row in self.index.pending_article_bodies():
                article_id = row["id"]
                url = public_article_url(row["url"])
                if not url or article_id in self._public_retries:
                    continue
                issue, raw = _download(url)
                if raw:
                    issue, text, canonical = _parse(raw)
                    if issue == "ok" and text:
                        self.index.save_article_body(article_id, text, "public_article", canonical or url)
                        return
                self._public_retries[article_id] = now + 6 * 3600
                if issue == "temporarily_unavailable":
                    self._public_wait = now + 3600
                return
        finally:
            self._read_slot.release()

    @staticmethod
    def _page(article, offset, source, text, url, issue):
        truncated = len(text) > ARTICLE_MAX_CHARS
        text = text[:ARTICLE_MAX_CHARS]
        end = offset + ARTICLE_PAGE_CHARS
        content = text[offset:end]
        return {key: article[key] for key in ("id", "title", "publisher", "published_on", "published_at")} | {
            "url": url, "state": "excerpt" if source == "indexed_excerpt" and content else ("text" if content else "unavailable"),
            "content_source": source if content else None, "content": content, "offset": offset,
            "next_offset": end if end < len(text) else None, "truncated": truncated,
            "issue": issue or ("no_more_text" if offset and not content else None),
        }
