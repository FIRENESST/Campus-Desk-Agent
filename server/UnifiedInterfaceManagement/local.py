"""Local JSON Lines transport for the same HTTP-shaped request and response."""

import asyncio
import base64
import json
import re
from urllib.parse import unquote_to_bytes, urlsplit

from protocol import MAX_RAG_UPLOAD, InterfaceError, parse_json


MAX_LINE = 4 * ((MAX_RAG_UPLOAD + 2) // 3) + 32 * 1024
FRAME_BYTES = 64 * 1024
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
INVALID_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
BLOCKED_HEADERS = frozenset((b"host", b"content-length", b"transfer-encoding", b"connection"))
FIELDS = frozenset(("id", "method", "path", "headers", "json", "body", "body_base64"))


def _headers(value) -> list[tuple[bytes, bytes]]:
    entries = list(value.items()) if isinstance(value, dict) else value
    if not isinstance(entries, list) or len(entries) > 64:
        raise ValueError
    result = [(b"host", b"127.0.0.1")]
    length = 0
    for entry in entries:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ValueError
        name, content = entry
        if not isinstance(name, str) or not TOKEN.fullmatch(name) or not isinstance(content, str):
            raise ValueError
        key, raw = name.lower().encode("ascii"), content.encode("latin-1")
        if key in BLOCKED_HEADERS or any(char < 32 and char != 9 or char == 127 for char in raw):
            raise ValueError
        length += len(key) + len(raw)
        if length > 16 * 1024:
            raise ValueError
        result.append((key, raw))
    return result


def _request(message: dict, scheme: str) -> tuple[dict, bytes]:
    if message.keys() - FIELDS:
        raise ValueError
    method, target = message.get("method"), message.get("path")
    if not isinstance(method, str) or len(method) > 16 or not TOKEN.fullmatch(method):
        raise ValueError
    if (
        not isinstance(target, str) or not target.startswith("/") or target.startswith("//")
        or any(not 33 <= ord(char) <= 126 or char in "\\#" for char in target)
        or len(target) > 8_192 or INVALID_ESCAPE.search(target)
    ):
        raise ValueError
    url = urlsplit(target)
    path = unquote_to_bytes(url.path).decode("utf-8")
    if any(char in "\\?#" or ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError

    headers = _headers(message.get("headers", {}))
    body_fields = message.keys() & {"json", "body", "body_base64"}
    if len(body_fields) > 1:
        raise ValueError
    if "json" in body_fields:
        body = json.dumps(message["json"], ensure_ascii=False, allow_nan=False).encode("utf-8")
        if not any(key == b"content-type" for key, _ in headers):
            headers.append((b"content-type", b"application/json"))
    elif "body" in body_fields:
        if not isinstance(message["body"], str):
            raise ValueError
        body = message["body"].encode("utf-8")
    elif "body_base64" in body_fields:
        if not isinstance(message["body_base64"], str):
            raise ValueError
        body = base64.b64decode(message["body_base64"], validate=True)
    else:
        body = b""
    if len(body) > MAX_RAG_UPLOAD:
        raise InterfaceError(413, "request_too_large", "本地请求体超过大小限制")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": scheme,
        "method": method.upper(),
        "path": path,
        "raw_path": url.path.encode("ascii"),
        "query_string": url.query.encode("ascii"),
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 0),
    }
    return scope, body


def _write(output, request_id, kind, **fields):
    frame = {"id": request_id, "type": kind, **fields}
    output.write(json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
    output.flush()


def _error(output, request_id, error: InterfaceError):
    _write(output, request_id, "error", status=error.status, **error.payload())


async def _exchange(app, output, raw: bytes, scheme: str):
    request_id = None
    try:
        message = parse_json(raw)
        if not isinstance(message, dict):
            raise ValueError
        identifier = message.get("id")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
            raise ValueError
        request_id = identifier
        scope, body = _request(message, scheme)
    except InterfaceError as exc:
        _error(output, request_id, exc)
        return
    except (ValueError, RecursionError):
        _error(output, request_id, InterfaceError(400, "invalid_envelope", "本地请求封装无效"))
        return

    sent_request = False
    completed = False
    pending = asyncio.Event()

    async def receive():
        nonlocal sent_request
        if sent_request:
            await pending.wait()
            return {"type": "http.disconnect"}
        sent_request = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        nonlocal completed
        if message["type"] == "http.response.start":
            headers = [[name.decode("ascii"), value.decode("latin-1")] for name, value in message.get("headers", ())]
            _write(output, request_id, "start", status=message["status"], headers=headers)
        elif message["type"] == "http.response.body":
            data = message.get("body", b"")
            if scope["method"] != "HEAD":
                for offset in range(0, len(data), FRAME_BYTES):
                    block = data[offset:offset + FRAME_BYTES]
                    _write(output, request_id, "data", body_base64=base64.b64encode(block).decode("ascii"))
            if not message.get("more_body", False):
                _write(output, request_id, "end")
                completed = True

    try:
        await app(scope, receive, send)
    except BrokenPipeError:
        raise
    except Exception:
        # A failed streaming response may already have emitted a start/data frame.
        if not completed:
            _error(output, request_id, InterfaceError(502, "response_interrupted", "响应未完整传输"))


async def serve_local(app, reader, output, *, scheme: str = "http"):
    async with app.router.lifespan_context(app):
        try:
            while raw := await asyncio.to_thread(reader.readline, MAX_LINE + 1):
                if len(raw) > MAX_LINE:
                    _error(output, None, InterfaceError(413, "line_too_large", "本地请求行超过大小限制"))
                    return
                if raw.strip():
                    await _exchange(app, output, raw, scheme)
        except BrokenPipeError:
            return
