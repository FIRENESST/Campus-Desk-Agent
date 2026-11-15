"""Loopback-only administration of the CAS account directory."""

import sqlite3
from html import escape
from ipaddress import ip_address
from pathlib import Path
from string import Template
from urllib.parse import parse_qsl, urlencode

from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .account_import import MAX_UPLOAD_BYTES, read_account_table
from .store import ADMIN_SESSION_SECONDS, CASAccountExists, CASAccountNotFound


ADMIN_COOKIE = "campus-cas-admin"
ADMIN_HOSTS = {"127.0.0.1:8193", "localhost:8193", "[::1]:8193"}
ASSETS = Path(__file__).with_name("web")
ADMIN_PAGE = Template((ASSETS / "admin.html").read_text(encoding="utf-8"))
ADMIN_SCRIPT = (ASSETS / "admin.js").read_bytes()
ADMIN_STYLE = (ASSETS / "admin.css").read_bytes()
PAGE_SIZE = 30
SAVED = {"add": "账号已创建。", "password": "密码已重置。", "number": "学工号已更换。",
         "enable": "账号已启用。", "disable": "账号已停用。", "import": "账号已批量导入。"}
DB_UNAVAILABLE = "CAS 账号数据库暂时无法写入，请稍后重试，并查看管理台日志。"
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
}
PASSWORD_FIELDS = """
  <label>新密码 <input name="password" type="password" autocomplete="new-password" required minlength="8" maxlength="128"></label>
  <label>确认密码 <input name="confirmation" type="password" autocomplete="new-password" required minlength="8" maxlength="128"></label>
"""
LOGIN_FORM = """
  <form method="post" action="/admin/login" class="admin-login">
    <label>管理员账号 <input name="username" autocomplete="username" required maxlength="32"></label>
    <div>
      <label class="password-label" for="cas-admin-password">密码</label>
      <div class="password-input" data-password-control>
        <input id="cas-admin-password" name="password" type="password" autocomplete="current-password" required maxlength="128">
        <button type="button" hidden>显示</button>
      </div>
    </div>
    <button>登录</button>
  </form>
"""


async def _body(request, media, limit):
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != media:
        raise ValueError("请求格式不正确。")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise ValueError("提交内容超过大小限制。")
        data.extend(chunk)
    return bytes(data)


async def _form(request):
    raw = await _body(request, "application/x-www-form-urlencoded", 4096)
    try:
        pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=6, errors="strict")
    except ValueError:
        raise ValueError("表单格式不正确。") from None
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("表单字段重复。")
    return result


def _page(content, message="", status=200, *, headers=None, signed_in=False):
    feedback = (f'<p class="feedback{" error" if status >= 400 else ""}" role="status">{escape(message)}</p>'
                if message else "")
    logout = '<form method="post" action="/admin/logout"><button class="secondary">退出管理</button></form>' if signed_in else ""
    return HTMLResponse(ADMIN_PAGE.substitute(content=content, feedback=feedback, logout=logout,
                                             layout="directory" if signed_in else "sign-in"), status,
                        headers={**HEADERS, **(headers or {})})


def _directory_url(query="", offset=0, edit=""):
    parameters = {key: value for key, value in {"q": query, "offset": offset, "edit": edit}.items() if value}
    return "/admin" + ("?" + urlencode(parameters) if parameters else "")


def _account_status(row):
    if not row["is_campus_number"]:
        return "待分配学工号", "pending"
    return ("启用", "enabled") if row["enabled"] else ("停用", "disabled")


def _account_forms(row):
    name = escape(row["username"], quote=True)
    action, label = ("disable", "停用") if row["enabled"] else ("enable", "启用")
    account = f'<input type="hidden" name="username" value="{name}">'
    return f"""
      <section class="card edit-account" id="account-form" aria-labelledby="edit-heading">
        <div class="section-head"><h2 id="edit-heading">编辑账号 <bdi>{name}</bdi></h2><a href="#account-directory">返回列表</a></div>
        <div class="edit-grid">
          <form method="post" action="/admin/accounts" data-password-form>
            {account}
            <input type="hidden" name="action" value="password">
            <h3>重置密码</h3>
            {PASSWORD_FIELDS}
            <button>重置密码</button>
          </form>
          <form method="post" action="/admin/accounts">
            {account}
            <input type="hidden" name="action" value="number">
            <h3>更换学工号</h3>
            <label>新学工号 <input name="campus_number" required minlength="2" maxlength="32" autocapitalize="none" spellcheck="false"></label>
            <button>更换学工号</button>
          </form>
          <form method="post" action="/admin/accounts">
            {account}
            <input type="hidden" name="action" value="{action}">
            <h3>账号状态</h3>
            <p class="muted">停用账号会结束该账号已有的 CAS 会话。</p>
            <button class="secondary">{label}账号</button>
          </form>
        </div>
      </section>
    """


def _account_table(rows, query, offset, more):
    entries = []
    for row in rows:
        status, style = _account_status(row)
        name = escape(row["username"])
        edit = escape(_directory_url(query, offset, row["username"]) + "#account-form", quote=True)
        entries.append(f'<tr><th scope="row">{name}</th><td><span class="badge {style}">{status}</span></td>'
                       f'<td>{row["active_sessions"]}</td><td><a href="{edit}" aria-label="编辑账号 {name}">编辑</a></td></tr>')
    rows_html = "".join(entries) or '<tr><td colspan="4" class="muted">没有匹配的学工号账号。</td></tr>'
    previous = escape(_directory_url(query, max(0, offset - PAGE_SIZE)) + "#account-directory", quote=True)
    following = escape(_directory_url(query, offset + PAGE_SIZE) + "#account-directory", quote=True)
    return f"""
      <section class="card" id="account-directory" aria-labelledby="directory-heading">
        <div class="section-head directory-head">
          <h2 id="directory-heading">学工号账号</h2>
          <form action="/admin" method="get" class="search-form" role="search">
            <input name="q" type="search" value="{escape(query, quote=True)}" maxlength="64" aria-label="查找学工号" placeholder="查找学工号">
            <button class="secondary">查询</button>
          </form>
        </div>
        <div class="table-scroll" tabindex="0" role="region" aria-label="CAS 账号列表">
          <table><thead><tr><th>学工号</th><th>状态</th><th>有效会话</th><th>操作</th></tr></thead>
          <tbody>{rows_html}</tbody></table>
        </div>
        <nav class="pagination" aria-label="账号分页">
          {f'<a href="{previous}">上一页</a>' if offset else ''}
          <span>第 {offset // PAGE_SIZE + 1} 页</span>
          {f'<a href="{following}">下一页</a>' if more else ''}
        </nav>
      </section>
    """


def admin_routes(store, event_log):
    def local(handler):
        async def guarded(request):
            try:
                loopback = ip_address(request.client.host).is_loopback
            except (AttributeError, ValueError):
                loopback = False
            host = request.headers.get("host", "").lower()
            if not loopback or host not in ADMIN_HOSTS:
                return Response(status_code=404, headers=HEADERS)
            if request.method == "POST" and request.headers.getlist("origin") != [f"http://{host}"]:
                return Response(status_code=403, headers=HEADERS)
            try:
                return await handler(request)
            except sqlite3.Error as exc:
                event_log.record(f"CAS 管理请求失败（{_sqlite_problem(exc)}）。", "error")
                message = "CAS 账号数据库暂不可用，请稍后重试。"
                if handler in (logs, import_accounts):
                    return JSONResponse({"message": message}, 503, HEADERS)
                return _page('<a href="/admin">返回管理台</a>', message, 503)
        return guarded

    async def active(request):
        return await run_in_threadpool(store.administrator_session_active, request.cookies.get(ADMIN_COOKIE, ""))

    async def directory(message="", status=200, *, query="", offset=0, edit="", username=""):
        try:
            rows = await run_in_threadpool(store.accounts, search=query.lower(), limit=PAGE_SIZE + 1, offset=offset)
            selected = await run_in_threadpool(store.accounts, username=edit.lower()) if edit else []
        except sqlite3.Error:
            event_log.record("读取账号列表失败，CAS 账号数据库暂不可用。", "error")
            rows, selected = [], []
            if not message:
                message, status = "暂时无法读取账号列表，请稍后刷新。", 503
        if edit and not selected and not message:
            message, status = "找不到要编辑的学工号。", 404
        return _page(f"""
          <div class="account-tools">
            <section class="card">
              <h2>创建账号</h2>
              <form method="post" action="/admin/accounts" data-password-form>
                <input type="hidden" name="action" value="add">
                <label>学工号 <input name="username" value="{escape(username, quote=True)}" required minlength="2" maxlength="32" autocapitalize="none" spellcheck="false" aria-describedby="number-help"></label>
                <small id="number-help" class="muted">2–32 位，至少含一个数字。可包含英文字母、数字、点、下划线和连字符。</small>
                {PASSWORD_FIELDS}
                <small class="muted">密码至少 8 位，需包含数字和小写英文字母。</small>
                <button type="submit">创建</button>
              </form>
            </section>
            <section class="card">
              <h2>批量导入</h2>
              <p class="muted">首行填写「学工号」和「密码」。每次最多导入 100 个账号，文件最大 256 KiB。</p>
              <form id="cas-import">
                <label>选择表格 <input name="file" type="file" accept=".csv,.xlsx" required></label>
                <button>导入表格</button>
                <output aria-live="polite"></output>
              </form>
              <a href="/admin/template.csv" download>下载 CSV 模板</a>
            </section>
          </div>
          {_account_forms(selected[0]) if selected else ''}
          {_account_table(rows[:PAGE_SIZE], query, offset, len(rows) > PAGE_SIZE)}
          <section class="card" aria-labelledby="log-heading">
            <div class="section-head"><h2 id="log-heading">CAS 运行日志</h2><span id="log-state" class="muted">每 2 秒刷新</span></div>
            <ol id="cas-log" class="log-list" tabindex="0" aria-label="最近的 CAS 运行日志"></ol>
          </section>
        """, message, status, signed_in=True)

    async def index(request):
        if not await active(request):
            return _page(LOGIN_FORM, "请使用 CAS 管理员账号登录。")
        fields = request.query_params
        offset = fields.get("offset", "")
        offset = int(offset) if offset.isascii() and offset.isdecimal() and len(offset) <= 7 else 0
        return await directory(SAVED.get(fields.get("updated"), ""), query=fields.get("q", "").strip()[:64],
                               offset=offset, edit=fields.get("edit", "").strip()[:64])

    async def login(request):
        try:
            fields = await _form(request)
            if set(fields) != {"username", "password"} or not 1 <= len(fields["password"]) <= 128:
                raise ValueError("请填写管理员账号和密码。")
        except ValueError as exc:
            return _page(LOGIN_FORM, str(exc), 422)
        session, retry = await run_in_threadpool(store.administrator_login,
            fields["username"].strip().lower(), fields["password"], request.cookies.get(ADMIN_COOKIE, ""))
        if retry:
            event_log.record("CAS 管理员登录请求过于频繁。", "warning")
            return _page(LOGIN_FORM, "登录请求过于频繁，请稍后重试。", 429, headers={"Retry-After": str(retry)})
        if session is None:
            event_log.record("CAS 管理员登录校验失败。", "warning")
            return _page(LOGIN_FORM, "管理员账号或密码不正确。", 401)
        response = RedirectResponse("/admin", 303, HEADERS)
        response.set_cookie(ADMIN_COOKIE, session, max_age=ADMIN_SESSION_SECONDS, path="/admin", httponly=True, samesite="strict")
        event_log.record("CAS 管理员已登录。")
        return response

    async def logout(request):
        if await active(request):
            event_log.record("CAS 管理员已退出。")
        await run_in_threadpool(store.administrator_logout, request.cookies.get(ADMIN_COOKIE, ""))
        response = RedirectResponse("/admin", 303, HEADERS)
        response.delete_cookie(ADMIN_COOKIE, path="/admin", httponly=True, samesite="strict")
        return response

    async def accounts(request):
        if not await active(request):
            return _page(LOGIN_FORM, "管理会话已过期，请登录后重新提交账号表单。", 401)
        action, name = "", ""
        try:
            fields = await _form(request)
            action, name = fields.pop("action", ""), fields.pop("username", "")
            if action in {"add", "password"} and set(fields) == {"password", "confirmation"}:
                if fields["password"] != fields["confirmation"]:
                    raise ValueError("两次输入的密码不一致。")
                change = store.add if action == "add" else store.change_password
                await run_in_threadpool(change, name, fields["password"])
            elif action == "number" and set(fields) == {"campus_number"}:
                await run_in_threadpool(store.set_campus_number, name, fields["campus_number"])
            elif action in {"enable", "disable"} and not fields:
                await run_in_threadpool(store.set_enabled, name, action == "enable")
            else:
                raise ValueError("请选择有效的账号操作。")
        except CASAccountExists as exc:
            return await account_error(action, name, str(exc), 409, "学工号已存在")
        except CASAccountNotFound as exc:
            return await account_error(action, name, str(exc), 404, "账号不存在")
        except ValueError as exc:
            return await account_error(action, name, str(exc), 422, "表单校验失败")
        except sqlite3.Error as exc:
            return await account_error(action, name, DB_UNAVAILABLE, 503, _sqlite_problem(exc))
        event_log.record(SAVED[action])
        return RedirectResponse(f"/admin?updated={action}", 303, HEADERS)

    async def account_error(action, name, message, status, reason):
        description = {"add": "创建账号", "number": "更换学工号", "password": "重置密码",
                       "enable": "启用账号", "disable": "停用账号"}.get(action, "账号操作")
        event_log.record(f"{description}失败（{reason}）。", "error" if status == 503 else "warning")
        return await directory(message, status, username=name[:32] if action == "add" else "",
                               edit=name[:64] if action != "add" else "")

    async def import_accounts(request):
        if not await active(request):
            return JSONResponse({"message": "请重新登录 CAS 管理台。"}, 401, HEADERS)
        try:
            fields = request.query_params.multi_items()
            if len(fields) != 1 or fields[0][0] != "format" or fields[0][1] not in {"xlsx", "csv"}:
                raise ValueError("请选择 .xlsx 或 UTF-8 .csv 表格。")
            raw = await _body(request, "application/octet-stream", MAX_UPLOAD_BYTES)
            rows = await run_in_threadpool(read_account_table, raw, fields[0][1])
            event_log.record(f"开始导入 {len(rows)} 个 CAS 账号。")
            await run_in_threadpool(store.add_many, [(row.username, row.password) for row in rows])
        except CASAccountExists as exc:
            row = next(row for row in rows if row.username == exc.username)
            event_log.record(f"批量导入失败（第 {row.number} 行学工号已存在）。", "warning")
            return JSONResponse({"message": f"第 {row.number} 行的学工号已被占用。"}, 409, HEADERS)
        except ValueError as exc:
            event_log.record("批量导入失败（表格校验失败）。", "warning")
            return JSONResponse({"message": str(exc)}, 422, HEADERS)
        except sqlite3.Error as exc:
            event_log.record(f"批量导入失败（{_sqlite_problem(exc)}）。", "error")
            return JSONResponse({"message": DB_UNAVAILABLE}, 503, HEADERS)
        event_log.record(f"已批量创建 {len(rows)} 个 CAS 账号。")
        return JSONResponse({"created": len(rows)}, 201, HEADERS)

    async def logs(request):
        if not await active(request):
            return JSONResponse({"message": "请重新登录 CAS 管理台。"}, 401, HEADERS)
        return JSONResponse({"items": event_log.recent()}, headers=HEADERS)

    async def script(request):
        return Response(ADMIN_SCRIPT, media_type="text/javascript", headers=HEADERS)

    async def style(request):
        return Response(ADMIN_STYLE, media_type="text/css", headers=HEADERS)

    async def template(request):
        return Response("\ufeff学工号,密码\r\n", media_type="text/csv",
                        headers={**HEADERS, "Content-Disposition": 'attachment; filename="cas-accounts.csv"'})

    return [
        Route("/admin", local(index)),
        Route("/admin/login", local(login), methods=["POST"]),
        Route("/admin/logout", local(logout), methods=["POST"]),
        Route("/admin/accounts", local(accounts), methods=["POST"]),
        Route("/admin/import", local(import_accounts), methods=["POST"]),
        Route("/admin/logs", local(logs)),
        Route("/admin/admin.js", local(script)),
        Route("/admin/admin.css", local(style)),
        Route("/admin/template.csv", local(template)),
    ]


def _sqlite_problem(error):
    code = getattr(error, "sqlite_errorcode", 0) & 0xff
    return "数据库正被占用" if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} else "数据库写入失败"
