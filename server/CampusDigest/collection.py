"""Adapters for campus sites, article lists and local report imports."""

import json
import re
from email.utils import parsedate_to_datetime
from functools import partial
from html.parser import HTMLParser
from time import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from .index import _item
from .polling import Entry, Page, PollUnavailable, Source, source_key


_MAX_PAGE_BYTES = 4 * 1024 * 1024
_NEXT = {"下一页", "下页", "next", "›", "»", ">"}
_SUFFIX = re.compile(r"/list(?:[0-9]+)?\.(?:htm|html)$", re.I)


class ListLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.href = None
        self.label = ""
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            self.href = attrs.get("href")
            self.label = " ".join(attrs.get(name, "") or "" for name in ("rel", "class", "title", "aria-label"))

    def handle_data(self, data):
        if self.href:
            self.label += " " + data

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.links.append((self.href, self.label.strip()))
            self.href = None


def _next_page(html, current, first):
    parser = ListLinks()
    parser.feed(html)
    base = urlsplit(first)
    for href, label in parser.links:
        if not (set(label.casefold().split()) & _NEXT or "下一页" in label):
            continue
        url = urljoin(current, href)
        parts = urlsplit(url)
        if (parts.scheme, parts.netloc) != (base.scheme, base.netloc) or parts.fragment or url in (current, first):
            continue
        if _SUFFIX.sub("/list.htm", parts.path) != _SUFFIX.sub("/list.htm", base.path):
            continue
        return url
    return None


def _request(url, headers):
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=6) as reply:
            data = reply.read(_MAX_PAGE_BYTES + 1)
            encoding = reply.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        if exc.code in {429, 503}:
            retry_after = exc.headers.get("Retry-After", "")
            exc.close()
            if retry_after.isascii() and retry_after.isdecimal() and len(retry_after) < 10:
                delay = int(retry_after)
            else:
                try:
                    delay = parsedate_to_datetime(retry_after).timestamp() - time()
                except (OverflowError, ValueError, OSError):
                    delay = 60
            raise PollUnavailable(delay, entire_source=True) from None
        exc.close()
        raise
    if len(data) > _MAX_PAGE_BYTES:
        raise ValueError("日报来源页面超过大小上限")
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return data.decode("gb18030", errors="replace")


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def _entries(rows):
    unique = {}
    for row in rows:
        if item := _item(row, official=False):
            unique.setdefault(item["id"], Entry(item["id"], item))
    return tuple(unique.values())


def _website(collect, first, name, cursor):
    url = cursor or first
    html = _request(url, {"User-Agent": collect.UA})
    rows = [{**row, "account": name} for row in collect.crawl_list(html, url)]
    return Page(_entries(rows), _next_page(html, url, first))


def _wechat_link(collect, url, name, _cursor):
    html = _request(url, {"User-Agent": collect.UA_MOBILE_WX})
    row = _parse_article(collect, html, url)
    if not row:
        raise ValueError("微信文章内容不可用")
    return Page(_entries([{**row, "account": name, "kind": "公众号"}]))


def _parse_article(collect, content, url):
    title = re.search(r'var msg_title\s*=\s*[\'"](.*?)[\'"]|<h1[^>]*id="activity-name"[^>]*>(.*?)</h1>',
                      content, re.S)
    if not title:
        return None
    timestamp = re.search(r'\bvar ct\s*=\s*[\'"]?(\d+)\b', content)
    body = re.search(r'id="js_content"[^>]*>(.*?)</div>\s*</div>', content, re.S)
    return {"title": collect.unescape(collect.strip_tags(next(value for value in title.groups() if value is not None))),
            "ts": timestamp[1] if timestamp else None, "url": url,
            "excerpt": collect.unescape(collect.strip_tags(body[1])) if body else ""}


def _manual(path, _cursor):
    rows = _read_json(path, [])
    if not isinstance(rows, list):
        raise ValueError("校园日报补录格式无效")
    return Page(_entries(rows))


class SourceCatalog:
    def __init__(self, collect):
        self.collect = collect
        self._config_stamp = None
        self._public = []

    def public_sources(self):
        filename = self.collect.CONFIG / "sources.json"
        try:
            stamp = filename.stat().st_mtime_ns
        except FileNotFoundError:
            stamp = 0
        if stamp == self._config_stamp:
            return self._public
        config = _read_json(filename, {})
        public = {}
        for group in config.get("web_sources", ()):
            if group.get("enabled", True):
                base, name = group["url"], group["name"]
                for url in (base, *(urljoin(base, path) for path in group.get("extra_paths", ()))):
                    key = source_key("web", url)
                    public[key] = Source(key, partial(_website, self.collect, url, name))
        for group in config.get("wechat_accounts", ()):
            if group.get("enabled"):
                for url in group.get("article_urls", ()):
                    key = source_key("article", url)
                    public[key] = Source(key, partial(_wechat_link, self.collect, url, group["name"]))
        if seed := config.get("manual_seed"):
            path = self.collect.CONFIG / seed
            key = source_key("manual", str(path))
            public[key] = Source(key, partial(_manual, path))
        self._public = list(public.values())
        self._config_stamp = stamp
        return self._public
