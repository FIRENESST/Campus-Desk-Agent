"""Read public HTTPS homepages for both the campus skill and the MCP tool."""

import asyncio
import codecs
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from ipaddress import IPv6Address, ip_address
from typing import Annotated

import httpx2
from pydantic import AfterValidator, BaseModel, Field


_MAX_REDIRECTS = 4
_MAX_BYTES = 512 * 1024
_MAX_TEXT_CHARS = 8_000
_MAX_TITLE_CHARS = 300
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_META_CHARSET = re.compile(rb"""<meta\b[^>]*?\bcharset\s*=\s*["']?\s*([a-z0-9_.-]+)""", re.I)
_WHITESPACE = re.compile(r"\s+")
_INVALID_TEXT = re.compile(r"[\x00\ud800-\udfff]")


class DomainPageError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _domain(value: str) -> str:
    message = "请提供公开域名，例如 www.example.org"
    try:
        host = httpx2.URL(host=value.strip().removesuffix(".")).raw_host.decode("ascii")
    except httpx2.InvalidURL:
        raise DomainPageError("invalid_domain", message) from None
    labels = host.split(".")
    if (
        len(host) > 253 or len(labels) < 2 or not labels[-1][:1].isalpha()
        or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)
    ):
        raise DomainPageError("invalid_domain", message)
    return host


PublicDomain = Annotated[
    str, Field(strict=True, min_length=1, max_length=254, description="公开域名，例如 www.example.org"),
    AfterValidator(_domain),
]


class DomainPageResult(BaseModel):
    domain: str
    source_url: str
    title: str
    text: str
    truncated: bool


@dataclass(frozen=True)
class PublicResource:
    url: httpx2.URL
    body: bytes
    media_type: str
    charset: str | None
    truncated: bool


async def _public_addresses(host: str) -> list[str]:
    try:
        records = await asyncio.get_running_loop().getaddrinfo(f"{host}.", 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise DomainPageError("dns_error", "无法解析目标域名") from None

    addresses = []
    for _, _, _, _, endpoint in records:
        address = ip_address(endpoint[0])
        if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if (
            not address.is_global or address.is_multicast or address.is_reserved
            or (isinstance(address, IPv6Address) and address.is_site_local)
        ):
            raise DomainPageError("non_public_address", "目标域名解析到了非公网地址")
        addresses.append(str(address))
    if not addresses:
        raise DomainPageError("dns_error", "目标域名没有可连接的地址")
    return list(dict.fromkeys(addresses))[:4]


async def _read_response(
    url: httpx2.URL, response: httpx2.Response, media_types: tuple[str, ...], max_bytes: int,
) -> PublicResource | httpx2.URL:
    if response.status_code in _REDIRECT_CODES:
        location = response.headers.get("location")
        if not location or len(location) > 2_048:
            raise DomainPageError("invalid_redirect", "站点没有提供有效的跳转地址")
        return url.join(location)
    if not response.is_success:
        raise DomainPageError("http_error", f"站点返回 HTTP {response.status_code}")

    media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type not in media_types:
        raise DomainPageError("unsupported_content", "站点返回了不支持的内容类型")
    if response.headers.get("content-encoding", "").strip().lower() not in {"", "identity"}:
        raise DomainPageError("unsupported_encoding", "站点返回了未请求的压缩内容")

    body = bytearray()
    limit = max_bytes + 1
    async for chunk in response.aiter_raw(chunk_size=16 * 1024):
        body.extend(chunk[:limit - len(body)])
        if len(body) == limit:
            break
    truncated = len(body) > max_bytes
    if truncated:
        del body[max_bytes:]
    return PublicResource(url, bytes(body), media_type, response.charset_encoding, truncated)


async def _request_resource(
    url: httpx2.URL, media_types: tuple[str, ...], max_bytes: int,
) -> PublicResource | httpx2.URL:
    if url.scheme != "https" or url.port is not None or url.userinfo:
        raise DomainPageError("invalid_redirect", "目标需要使用公开的 HTTPS 地址和标准端口")
    host = _domain(url.raw_host.decode("ascii"))
    url = url.copy_with(host=host, fragment=None)
    addresses = await _public_addresses(host)

    # A fresh client per hop keeps cookies and pooled connections scoped to that hop.
    async with httpx2.AsyncClient(
        timeout=httpx2.Timeout(5, connect=3), follow_redirects=False, trust_env=False,
    ) as client:
        for address in addresses:
            try:
                # Connect to the checked IP while preserving the original TLS and HTTP host.
                async with client.stream(
                    "GET", url.copy_with(host=address),
                    headers={"Host": host, "Accept": ", ".join(media_types),
                             "Accept-Encoding": "identity"},
                    extensions={"sni_hostname": host},
                ) as response:
                    return await _read_response(url, response, media_types, max_bytes)
            except (httpx2.ConnectError, httpx2.ConnectTimeout):
                continue
    raise DomainPageError("connection_failed", "无法与目标站点建立 HTTPS 连接")


async def fetch_public_https(
    url: httpx2.URL, *, media_types: tuple[str, ...], max_bytes: int,
) -> PublicResource:
    """Download bounded data from HTTPS, checking and pinning DNS on every hop."""
    try:
        async with asyncio.timeout(12):
            for _ in range(_MAX_REDIRECTS + 1):
                result = await _request_resource(url, media_types, max_bytes)
                if isinstance(result, PublicResource):
                    return result
                url = result
    except (TimeoutError, httpx2.TimeoutException):
        raise DomainPageError("timeout", "读取站点超时") from None
    except httpx2.InvalidURL:
        raise DomainPageError("invalid_redirect", "站点返回了无效的跳转地址") from None
    except httpx2.RequestError:
        raise DomainPageError("network_error", "读取站点时连接中断或响应无效") from None
    raise DomainPageError("too_many_redirects", "站点跳转次数超过限制")


def _decode_page(page: PublicResource) -> str:
    charset = page.charset
    if page.body.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        charset = "utf-32"
    elif page.body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        charset = "utf-16"
    elif page.body.startswith(codecs.BOM_UTF8):
        charset = "utf-8-sig"
    elif page.media_type in _HTML_TYPES and not charset:
        match = _META_CHARSET.search(page.body[:4_096])
        if match:
            charset = match.group(1).decode("ascii")
    if charset and charset.lower() in {"gb2312", "gbk"}:
        charset = "gb18030"
    for encoding in (charset, "utf-8-sig", "gb18030"):
        if not encoding:
            continue
        try:
            return page.body.decode(encoding)
        except UnicodeDecodeError as error:
            if page.truncated and error.end == len(error.object):
                # A BOM-aware decoder may report positions after consuming the BOM.
                tail_size = error.end - error.start
                return page.body[:-tail_size].decode(encoding, errors="replace")
        except (LookupError, UnicodeError):
            continue
    return page.body.decode("utf-8", errors="replace")


class _PageText(HTMLParser):
    HIDDEN = {"script", "style", "noscript", "template", "svg"}
    BLOCKS = {
        "article", "aside", "blockquote", "br", "div", "footer", "header", "hr",
        "h1", "h2", "h3", "h4", "h5", "h6", "li", "main", "nav", "p", "pre", "section", "tr",
    }

    def __init__(self):
        super().__init__()
        self.title: list[str] = []
        self.body: list[str] = []
        self._hidden: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self._hidden.append(tag)
        elif self._hidden:
            return
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCKS:
            self.body.append("\n")
        elif tag in {"td", "th"}:
            self.body.append(" ")

    def handle_endtag(self, tag):
        if self._hidden:
            if tag == self._hidden[-1]:
                self._hidden.pop()
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCKS:
            self.body.append("\n")

    def handle_data(self, data):
        if self._hidden:
            return
        if self._in_title:
            self.title.append(data)
        else:
            self.body.append(_WHITESPACE.sub(" ", data))


def _extract_text(page: PublicResource) -> tuple[str, str]:
    text = _INVALID_TEXT.sub("", _decode_page(page))
    title = ""
    if page.media_type in _HTML_TYPES:
        parser = _PageText()
        try:
            parser.feed(text)
            parser.close()
        except (AssertionError, ValueError):
            raise DomainPageError("invalid_html", "首页的 HTML 无法解析") from None
        title = " ".join("".join(parser.title).split())
        text = "".join(parser.body)
    lines = (" ".join(line.split()) for line in text.splitlines())
    return title, "\n".join(filter(None, lines))


async def fetch_domain_page(domain: str) -> DomainPageResult:
    domain = _domain(domain)
    page = await fetch_public_https(
        httpx2.URL(scheme="https", host=domain, path="/"), media_types=(*_HTML_TYPES, "text/plain"),
        max_bytes=_MAX_BYTES,
    )
    title, text = _extract_text(page)
    if not text:
        raise DomainPageError("empty_text", "首页没有可提取的静态正文")
    return DomainPageResult(
        domain=domain,
        source_url=str(page.url),
        title=title[:_MAX_TITLE_CHARS],
        text=text[:_MAX_TEXT_CHARS].rstrip(),
        truncated=page.truncated or len(title) > _MAX_TITLE_CHARS or len(text) > _MAX_TEXT_CHARS,
    )
