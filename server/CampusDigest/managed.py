"""Start the campus digest package under the project supervisor."""

import argparse
import hmac
import json
import logging
import os
import re
import signal
import sqlite3
import sys
from pathlib import Path
from threading import BoundedSemaphore, Thread
from time import monotonic
from urllib.parse import parse_qsl, unquote, urlsplit

from .collection import SourceCatalog
from .index import DigestIndex
from .polling import POLL_SECONDS, run_polling
from .weread_admin import WeReadAdmin
from .weread_poll import WEREAD_POLL_SECONDS, LegacyOfficialCache, WeReadPoll
from .wechat_article import ARTICLE_MAX_CHARS, WeChatArticles, public_article_url


DEFAULT_INDEX = Path(__file__).resolve().parents[2] / "DataBase" / "CampusDigest" / "digest.sqlite3"
_SEARCH = "/api/campus-desk/search"
_REVISION = "/api/campus-desk/revision"
_WECHAT = "/api/campus-desk/wechat"
_WECHAT_EVENTS = "/api/campus-desk/wechat/events"
_WECHAT_ACCOUNTS = "/api/campus-desk/wechat/accounts"
_WECHAT_FEED = "/internal/wechat/following"
_WECHAT_ARTICLE = "/internal/wechat/articles/"
_WEREAD_STATUS = "/internal/weread/status"
_WEREAD_LOGIN = "/internal/weread/login"
_WECHAT_PAGE_SIZE = 12
_ACCOUNT_ID = re.compile(r"[0-9a-f]{64}\Z")
_LIST_COLLECTION_JOBS = {"official", "weread_slow"}
_PUBLICATION_JOBS = {"daily", "ingest"}


def _filters(query_string):
    pairs = parse_qsl(query_string, keep_blank_values=True, strict_parsing=True, max_num_fields=8, errors="strict")
    fields = dict(pairs)
    filters = {"account": 120, "title": 200, "url": 2048, "body": 200}
    if len(pairs) != len(fields) or fields.keys() - {"q", "source", "days", "limit", *filters}:
        raise ValueError
    query, source = fields.get("q", "").strip(), fields.get("source", "all")
    days, limit = int(fields.get("days", "30")), int(fields.get("limit", "6"))
    if len(query) > 200 or source not in {"all", "web", "wechat"} or not 0 <= days <= 36_500 or not 1 <= limit <= 8:
        raise ValueError
    selected = {key: fields[key].strip() for key in filters if key in fields}
    if any(len(value) > filters[key] for key, value in selected.items()) or selected and source != "wechat":
        raise ValueError
    if "url" in selected:
        selected["url"] = public_article_url(selected["url"])
        if not selected["url"]:
            raise ValueError
    return {"query": query, "source": source, "days": days, "limit": limit, **selected}


def _wechat_filters(query_string):
    pairs = parse_qsl(query_string, keep_blank_values=True, strict_parsing=True, max_num_fields=3, errors="strict")
    fields = dict(pairs)
    account, offset = fields.get("account", "").strip(), fields.get("offset", "0")
    title = fields.get("title", "").strip()
    if (
        len(pairs) != len(fields) or fields.keys() - {"account", "title", "offset"}
        or len(account) > 120 or len(title) > 200
        or not 1 <= len(offset) <= 6 or not offset.isascii() or not offset.isdecimal()
    ):
        raise ValueError
    return {"query": "", "source": "wechat", "days": 0, "limit": _WECHAT_PAGE_SIZE,
            "account": account, "title": title, "offset": int(offset)}


def _account_filters(query_string):
    pairs = parse_qsl(query_string, keep_blank_values=True, strict_parsing=True, max_num_fields=4, errors="strict")
    fields = dict(pairs)
    query, account_id = fields.get("q", "").strip(), fields.get("id", "")
    limit, offset = fields.get("limit", "30"), fields.get("offset", "0")
    if (len(fields) != len(pairs) or fields.keys() - {"q", "id", "limit", "offset"} or len(query) > 120
            or account_id and not _ACCOUNT_ID.fullmatch(account_id)
            or not (limit.isascii() and limit.isdecimal() and len(limit) <= 3 and 1 <= int(limit) <= 80)
            or not (offset.isascii() and offset.isdecimal() and len(offset) <= 6)):
        raise ValueError
    return {"query": query, "account_id": account_id, "limit": int(limit), "offset": int(offset)}


def _feed(index, filters, followed=()):
    data = index.read(**filters, followed=followed)
    data.update(page_size=_WECHAT_PAGE_SIZE, refresh_seconds=POLL_SECONDS, collection_interval_seconds=WEREAD_POLL_SECONDS)
    return data


def _handler(base, index, weread, runtime_key, articles):
    class Handler(base):
        def parse_request(self):
            if not super().parse_request():
                return False
            port = self.server.server_port
            hosts = self.headers.get_all("Host", [])
            if hosts not in ([f"127.0.0.1:{port}"], [f"localhost:{port}"]):
                self.send_error(403)
                return False
            if self.headers.get("Origin", f"http://{hosts[0]}") != f"http://{hosts[0]}":
                self.send_error(403)
                return False
            return True

        def do_GET(self):
            target = urlsplit(self.path)
            path = unquote(target.path)
            if path in {_WEREAD_STATUS, _WEREAD_LOGIN}:
                return self._weread(path, target.query)
            if path.startswith(_WECHAT_ARTICLE):
                return self._wechat_article(path[len(_WECHAT_ARTICLE):], target.query)
            if target.path not in {_SEARCH, _REVISION, _WECHAT, _WECHAT_EVENTS, _WECHAT_ACCOUNTS}:
                return super().do_GET()
            try:
                if target.path in {_REVISION, _WECHAT_EVENTS} and target.query:
                    raise ValueError
                if target.path == _WECHAT_EVENTS:
                    return self._wechat_events()
                parse = {_SEARCH: _filters, _WECHAT: _wechat_filters, _WECHAT_ACCOUNTS: _account_filters}.get(target.path)
                filters = parse(target.query) if parse else {}
            except ValueError:
                return self._json(400, {"error": {"code": "invalid_digest_query", "message": "校园日报查询参数无效。"}})
            try:
                if target.path == _WECHAT_ACCOUNTS:
                    data = index.wechat_accounts(**filters)
                elif target.path == _WECHAT:
                    data = _feed(index, filters)
                else:
                    data = index.read(**filters)
                return self._json(200, data)
            except (LookupError, sqlite3.Error):
                return self._json(503, {"error": {"code": "digest_unavailable", "message": "校园日报索引暂未就绪。"}})

        def do_POST(self):
            target = urlsplit(self.path)
            path = unquote(target.path)
            if path == _WEREAD_LOGIN:
                return self._weread(path, target.query)
            if path == _WECHAT_FEED:
                return self._following_feed(target.query)
            return super().do_POST()

        def _following_feed(self, query):
            if not self._runtime_authorized():
                return self.send_error(403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 512 * 1024:
                    raise ValueError
                filters = _wechat_filters(query)
                payload = json.loads(self.rfile.read(length))
                if (not isinstance(payload, dict) or set(payload) != {"following"}
                        or not isinstance(following := payload["following"], list)
                        or any(not isinstance(value, str) or not _ACCOUNT_ID.fullmatch(value) for value in following)):
                    raise ValueError
            except (ValueError, RecursionError, OSError):
                return self._json(400, {"error": {"code": "invalid_digest_query", "message": "公众号查询参数无效。"}})
            try:
                return self._json(200, _feed(index, filters, following))
            except (LookupError, sqlite3.Error):
                return self._json(503, {"error": {"code": "digest_unavailable", "message": "校园日报索引暂未就绪。"}})

        def _runtime_authorized(self):
            keys = self.headers.get_all("X-Campus-Runtime-Key", [])
            return bool(runtime_key and len(keys) == 1 and hmac.compare_digest(runtime_key.encode(), keys[0].encode()))

        def _wechat_article(self, article_id, query):
            if not self._runtime_authorized():
                return self.send_error(403)
            try:
                if not re.fullmatch(r"[0-9a-f]{64}", article_id):
                    raise ValueError
                if query and not re.fullmatch(r"offset=[0-9]{1,5}", query):
                    raise ValueError
                offset = int(query.partition("=")[2]) if query else 0
                if not 0 <= offset < ARTICLE_MAX_CHARS:
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": {"code": "invalid_article", "message": "文章编号或正文位置无效。"}})
            try:
                article = articles.read(article_id, offset)
            except (LookupError, OSError, sqlite3.Error):
                return self._json(503, {"error": {"code": "article_unavailable", "message": "暂时无法读取公众号文章。"}})
            if article is None:
                return self._json(404, {"error": {"code": "article_not_found", "message": "这篇文章未收录或已移出公众号列表。"}})
            return self._json(200, article)

        def _weread(self, path, query):
            if not self._runtime_authorized():
                return self.send_error(403)
            if query or self.headers.get_all("Content-Length", []) not in ([], ["0"]) or "Transfer-Encoding" in self.headers:
                return self.send_error(400)
            try:
                if self.command == "POST":
                    return self._json(202, weread.start())
                return self._json(200, weread.status(include_qr=path == _WEREAD_LOGIN))
            except (OSError, ValueError):
                return self._json(503, {"error": "weread_unavailable"})

        def _wechat_events(self):
            if not self.server.wechat_slots.acquire(blocking=False):
                return self._json(503, {"error": {"code": "digest_busy", "message": "公众号更新连接较多，请稍后重试。"}})
            self.close_connection = True
            try:
                self.connection.settimeout(5)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-transform")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                deadline = monotonic() + 90
                version = None
                while (remaining := deadline - monotonic()) > 0:
                    latest = index.wait_for_revision(version, min(15, remaining))
                    event = f"data: {latest}\n\n" if latest != version else ": keep-alive\n\n"
                    self.wfile.write(event.encode("ascii"))
                    self.wfile.flush()
                    version = latest
            except OSError:
                pass  # The browser or the proxy disconnected.
            finally:
                self.server.wechat_slots.release()

        def log_message(self, fmt, *args):
            path = urlsplit(getattr(self, "path", "")).path
            if not path.startswith(_WECHAT_ARTICLE) and path not in {
                _SEARCH, _REVISION, _WECHAT, _WECHAT_EVENTS, _WEREAD_STATUS, _WEREAD_LOGIN,
            }:
                super().log_message(fmt, *args)

    return Handler


def _runner(scheduler, index):
    class PublishingRunner(scheduler.Runner):
        def run(self, job, extra_env=None):
            if job.name in _LIST_COLLECTION_JOBS:
                self.tail.clear()
                self.tail.append("公众号文章列表由后台轮询持续更新。")
                return 0
            code = super().run(job, extra_env=extra_env)
            if job.name in _PUBLICATION_JOBS and not self.stopping.is_set():
                index.refresh()
            return code

    return PublishingRunner()


def _jobs(scheduler):
    return [job for job in scheduler.build_jobs() if job.name not in _LIST_COLLECTION_JOBS]


def _discover_wechat(collector, cache, index, stopping):
    while not stopping.is_set():
        collector.discover()
        try:
            cache.flush()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logging.getLogger(__name__).warning("校园日报写入原有公众号缓存失败（%s）", type(exc).__name__)
        index.refresh()
        stopping.wait(POLL_SECONDS)


def _fill_wechat_bodies(articles, stopping):
    while not stopping.is_set():
        try:
            articles.prefetch_public()
        except (OSError, sqlite3.Error) as exc:
            logging.getLogger(__name__).warning("公众号公开原文索引暂不可用（%s）", type(exc).__name__)
        stopping.wait(WEREAD_POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser(description="运行受主管理器管理的校园日报及其查询索引")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--port", type=int, default=os.getenv("CAMPUS_DIGEST_PORT") or "8184")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not (directory / "server" / "winctl.py").is_file() or not 1 <= args.port <= 65_535:
        parser.error("请检查校园日报目录与本机监听端口")
    os.environ["WEB_PORT"] = str(args.port)
    sys.path[:0] = [str(directory / "server"), str(directory / "pipeline")]
    import winctl
    import scheduler
    import serve
    import collect
    import official_collect
    import weread_login
    import weread_pool
    import weread_remote
    import weread_renew

    if winctl.running_pid():
        parser.error("校园日报已独立运行，请先用原程序停止它")
    lock = winctl.U.SingleRun(winctl.LOGS / ".campus-desk.lock")
    with lock.f:
        if not lock.ok:
            parser.error("已有受管校园日报进程在运行")
        index = DigestIndex(directory, args.index, serve.api)
        index.refresh()
        runner = _runner(scheduler, index)
        weread = WeReadAdmin(weread_pool.POOL.accounts, weread_login.jar_cookies, weread_renew.curl,
                            weread_remote.login_status, weread_remote.start_login)

        cache = LegacyOfficialCache(official_collect)
        collector = WeReadPoll(index, official_collect, weread_pool.POOL, weread_renew.curl, cache)
        articles = WeChatArticles(index, weread_pool.POOL.accounts)
        sources = SourceCatalog(collect)

        for event in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGINT)):
            signal.signal(event, runner.stop)

        with serve.make_server("127.0.0.1", args.port) as httpd:
            httpd.RequestHandlerClass = _handler(serve.Handler, index, weread, os.getenv("MPM_RUNTIME_STATUS_KEY", ""), articles)
            httpd.wechat_slots = BoundedSemaphore(64)
            pid = str(os.getpid())
            winctl.PID_FILE.write_text(pid, encoding="ascii")
            web = Thread(target=httpd.serve_forever, daemon=True)
            web.start()
            public = Thread(target=run_polling, daemon=True, args=(index, sources.public_sources, runner.stopping),
                            kwargs={"workers": 24})
            wechat = Thread(target=run_polling, daemon=True, args=(index, lambda: collector.sources, runner.stopping),
                            kwargs={"workers": 1, "ready_at": collector.ready_at})
            discover = Thread(target=_discover_wechat, daemon=True, args=(collector, cache, index, runner.stopping))
            bodies = Thread(target=_fill_wechat_bodies, daemon=True, args=(articles, runner.stopping))
            threads = (public, wechat, discover, bodies)
            for thread in threads:
                thread.start()
            print(f"校园日报已在 127.0.0.1:{args.port} 启动", flush=True)
            try:
                scheduler.run_forever(runner, _jobs(scheduler))
            finally:
                runner.stopping.set()
                httpd.shutdown()
                for thread in threads:
                    thread.join()
                try:
                    cache.flush(force=True)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    logging.getLogger(__name__).warning("校园日报写入原有公众号缓存失败（%s）", type(exc).__name__)
                if winctl.PID_FILE.is_file() and winctl.PID_FILE.read_text(encoding="ascii").strip() == pid:
                    winctl.PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
