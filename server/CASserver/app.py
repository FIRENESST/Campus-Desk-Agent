"""CAS login and validation for the campus website's registered callback."""

import asyncio
import hmac
import ipaddress
import re
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree as XML

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .admin import admin_routes
from .admin_log import CASAdminLog
from .store import CASStore, FLOW_SECONDS, SESSION_SECONDS, canonical_campus_number, token_is_valid


CAS_NAMESPACE = "http://www.yale.edu/tp/cas"
CALLBACK = "/v1/auth/sso/campus/callback"
FORM_MEDIA = "application/x-www-form-urlencoded"
ASSETS = Path(__file__).with_name("web")
LOGIN_PAGE = Template((ASSETS / "index.html").read_text(encoding="utf-8"))
LOGIN_STYLE = (ASSETS / "style.css").read_bytes()
PRIVACY_PAGE = (ASSETS / "privacy.html").read_text(encoding="utf-8")
PASSWORD_SCRIPT = (ASSETS / "password.js").read_bytes()
PASSWORD_STYLE = (ASSETS / "password.css").read_bytes()
XML.register_namespace("cas", CAS_NAMESPACE)


def _pairs(raw):
    if len(raw) > 4096:
        raise ValueError("CAS request too large")
    return parse_qsl(raw, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=4)


def _fields(raw):
    pairs = _pairs(raw)
    fields = dict(pairs)
    if len(pairs) != len(fields):
        raise ValueError("Repeated CAS parameter")
    return fields


def _request_address(request):
    try:
        return str(ipaddress.ip_address(request.headers.get("x-cas-client-ip", "")))
    except ValueError:
        return "local"


def _origin(value):
    url = urlsplit(value)
    secure = url.scheme == "https"
    if (not url.hostname or url.username is not None or url.path not in {"", "/"} or url.query or url.fragment
        or not (secure or url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"})):
        raise ValueError("CAS 需要固定的 HTTPS 源地址，本机回环允许 HTTP")
    host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
    default_port = 443 if secure else 80
    return f"{url.scheme}://{host}" + (f":{url.port}" if url.port not in {None, default_port} else "")


def _loopback(request):
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except (AttributeError, ValueError):
        return False


def _xml(account=None, error=None, *, attributes=False):
    def tag(name):
        return f"{{{CAS_NAMESPACE}}}{name}"

    root = XML.Element(tag("serviceResponse"))
    if account:
        success = XML.SubElement(root, tag("authenticationSuccess"))
        XML.SubElement(success, tag("user")).text = account["subject"]
        if attributes:
            values = XML.SubElement(success, tag("attributes"))
            XML.SubElement(values, tag("displayName")).text = account["username"]
            XML.SubElement(values, tag("campusNumber")).text = account["username"]
    else:
        XML.SubElement(root, tag("authenticationFailure"), code=error).text = "CAS validation failed"
    return Response(XML.tostring(root, encoding="utf-8", xml_declaration=True), media_type="application/xml",
                    headers={"Cache-Control": "no-store"})


def create_app(directory, public_url, *, clock=None, identity_key="", public_sites=None):
    default_website = _origin(public_url)
    sites = {default_website: public_url.rstrip("/")}
    for website_url, cas_url in (public_sites or {}).items():
        website, cas = _origin(website_url), _origin(cas_url)
        if (website == cas or not all(value.startswith("https://") for value in (website, cas))
            or sites.get(cas, website) != website or sites.get(website, website) != website):
            raise ValueError("CAS 和校园助手需要各自的 HTTPS 地址")
        sites.update({cas: website, website: website})
    patterns = {site: re.compile(re.escape(website + CALLBACK) + r"\?state=st_[A-Za-z0-9_-]{43}\Z")
                for site, website in sites.items()}
    store = CASStore(directory, clock=clock) if clock is not None else CASStore(directory)
    capacity = asyncio.Semaphore(4)
    event_log = CASAdminLog()

    def browser_headers(form_action, script_source="'none'"):
        return {
            "Cache-Control": "no-store",
            "Content-Security-Policy": f"default-src 'none'; style-src 'self'; script-src {script_source}; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action {form_action}",
            # A native form needs its Origin header for the website's same-origin check.
            "Referrer-Policy": "strict-origin",
            "X-Content-Type-Options": "nosniff",
        }

    def page(heading, message, form="", *, site=None, status=200, retry=0, redirects_to_callback=False):
        website = sites.get(site, default_website)
        form_action = "'self'"
        if redirects_to_callback and site != website:
            # Browsers can apply form-action to the 303 after a form submission.
            form_action += " " + website + CALLBACK
        headers = browser_headers(form_action, "'self'")
        if retry:
            headers["Retry-After"] = str(retry)
        return HTMLResponse(LOGIN_PAGE.substitute(
            heading=escape(heading), message=escape(message), form=form, website=escape(website, quote=True),
            message_class=" error" if status >= 400 else "", message_role=' role="alert"' if status >= 400 else "",
        ), status, headers)

    def cookie_name(site, name):
        return ("__Host-" if site.startswith("https://") else "") + name

    def set_cookie(response, site, name, value, age):
        name = cookie_name(site, name)
        secure = site.startswith("https://")
        response.set_cookie(name, value, max_age=age, secure=secure, httponly=True, samesite="lax")

    def redirect(service, ticket=None):
        location = f"{service}&ticket={ticket}" if ticket else service
        return RedirectResponse(location, 303, {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    def request_site(request):
        origins = request.headers.getlist("x-cas-site-origin")
        return origins[0] if _loopback(request) and len(origins) == 1 and origins[0] in sites else None

    def invalid(site=None):
        event_log.record("校园认证请求无效或已过期。", "warning")
        return page("登录链接无效", "请返回已接入的应用，重新发起校园登录。", site=site, status=400)

    def credentials(site, flow, error="", status=200):
        form = f"""
          <form method="post" action="/cas/login">
            <input type="hidden" name="lt" value="{flow}">
            <label for="username">学工号</label>
            <input id="username" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="32" placeholder="请输入学号或工号" required>
            <label for="password">密码</label>
            <div class="password-input" data-password-control>
              <input id="password" type="password" name="password" autocomplete="current-password" maxlength="128" required>
              <button type="button" hidden>显示</button>
            </div>
            <button type="submit">登录</button>
          </form>
        """
        response = page("统一身份认证", error or "学生使用学号，教师和职工使用工号登录。", form,
                        site=site, status=status, redirects_to_callback=True)
        set_cookie(response, site, "campus-cas-flow", flow, FLOW_SECONDS)
        return response

    async def login_page(site, service, renew, session, address, error=""):
        ticket, flow, retry = await run_in_threadpool(store.start_login, service, renew, session, address)
        if retry:
            event_log.record("校园登录请求过于频繁。", "warning")
            return page("请稍后再试", "登录请求过于频繁，请稍后重新发起。", site=site, status=429, retry=retry)
        if ticket:
            return redirect(service, ticket)
        return credentials(site, flow, error, 401 if error else 200)

    async def login(request):
        site = request_site(request)
        if site is None:
            return invalid()
        session = request.cookies.get(cookie_name(site, "campus-cas-session"), "")
        address = _request_address(request)
        if request.method == "GET":
            try:
                fields = _fields(request.url.query)
            except ValueError:
                return invalid(site)
            service = fields.get("service", "")
            if fields.keys() - {"service", "renew"} or not patterns[site].fullmatch(service):
                return invalid(site)
            if fields.get("renew", "false") not in {"true", "false"}:
                return invalid(site)
            return await login_page(site, service, fields.get("renew") == "true", session, address)

        if (request.headers.get("origin") != site
            or request.headers.get("content-type", "").split(";")[0].strip().lower() != FORM_MEDIA):
            return invalid(site)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return invalid(site)
        try:
            fields = _fields(body.decode("utf-8"))
            entered_number, password, flow = fields["username"], fields["password"], fields["lt"]
        except (ValueError, KeyError):
            return invalid(site)
        bound = request.cookies.get(cookie_name(site, "campus-cas-flow"), "")
        if (fields.keys() != {"username", "password", "lt"} or not 1 <= len(password) <= 128
            or not token_is_valid(flow, "LT-") or not token_is_valid(bound, "LT-")
            or not hmac.compare_digest(flow, bound)):
            return invalid(site)
        try:
            number = canonical_campus_number(entered_number)
        except ValueError:
            number = ""
        service, retry = await run_in_threadpool(store.consume_login, flow, number, address)
        if not service or not patterns[site].fullmatch(service):
            return invalid(site)
        if retry:
            event_log.record("校园登录请求过于频繁。", "warning")
            return page("请稍后再试", "登录请求过于频繁，请稍后重新发起。", site=site, status=429, retry=retry)
        if not number:
            event_log.record("校园登录失败（学工号格式无效）。", "warning")
            return await login_page(site, service, True, "", address, "请填写有效的学号或工号。")
        async with capacity:
            result = await run_in_threadpool(store.authenticate, number, password, service, session)
        if not result:
            event_log.record("校园登录失败（学工号或密码校验未通过）。", "warning")
            return await login_page(site, service, True, "", address, "学工号或密码不正确。")
        ticket, session = result
        event_log.record("校园账号已通过 CAS 认证。")
        response = redirect(service, ticket)
        set_cookie(response, site, "campus-cas-flow", "", 0)
        set_cookie(response, site, "campus-cas-session", session, SESSION_SECONDS)
        return response

    async def validate(request):
        try:
            pairs = _pairs(request.url.query)
        except ValueError:
            return _xml(error="INVALID_REQUEST")
        if len({value for key, value in pairs if key == "ticket"}) > 1:
            return _xml(error="INVALID_REQUEST")
        fields = dict(pairs)
        ticket = fields.get("ticket")
        if not token_is_valid(ticket, "ST-"):
            return _xml(error="INVALID_TICKET")
        service = fields.get("service", "")
        renew = fields.get("renew", "false")
        valid = len(pairs) == len(fields) and fields.keys() <= {"service", "ticket", "renew"} and renew in {"true", "false"}
        # A validation attempt consumes the presented ticket, including a mismatched service.
        account, error = await run_in_threadpool(store.validate, ticket, service if valid else "", renew == "true")
        if not valid or not service:
            return _xml(error="INVALID_REQUEST")
        if error:
            event_log.record("校园应用的服务票据校验未通过。", "warning")
        else:
            event_log.record("校园应用的服务票据校验通过。")
        return _xml(account, error, attributes=request.url.path.startswith("/cas/p3/"))

    async def logout(request):
        site = request_site(request)
        if site is None:
            return invalid()
        if request.method == "GET":
            return page("退出校园认证", "退出后，再次使用校园身份登录时需要重新输入密码。",
                        '<form method="post" action="/cas/logout"><button type="submit">退出</button></form>', site=site)
        if request.headers.get("origin") != site:
            return invalid(site)
        await run_in_threadpool(store.logout, request.cookies.get(cookie_name(site, "campus-cas-session"), ""))
        response = redirect("/")
        set_cookie(response, site, "campus-cas-flow", "", 0)
        set_cookie(response, site, "campus-cas-session", "", 0)
        return response

    async def landing(request):
        return page("校园统一身份认证", "请从校园助手发起校园登录。", site=request_site(request))

    async def stylesheet(request):
        return Response(LOGIN_STYLE, media_type="text/css", headers={"Cache-Control": "no-store"})

    async def privacy(request):
        return HTMLResponse(PRIVACY_PAGE, headers=browser_headers("'none'"))

    async def password_script(request):
        return Response(PASSWORD_SCRIPT, media_type="text/javascript", headers={"Cache-Control": "no-store"})

    async def password_style(request):
        return Response(PASSWORD_STYLE, media_type="text/css", headers={"Cache-Control": "no-store"})

    async def health(request):
        return Response('{"status":"ok","service":"cas-server"}', media_type="application/json")

    async def identity_status(request):
        keys = request.headers.getlist("x-campus-runtime-key")
        if not (_loopback(request) and identity_key and len(keys) == 1 and hmac.compare_digest(keys[0].encode(), identity_key.encode())):
            return Response(status_code=403)
        number = await run_in_threadpool(store.active_campus_number, request.path_params["subject"])
        headers = {"Cache-Control": "no-store"}
        if not number:
            return Response(status_code=404, headers=headers)
        return JSONResponse({"campus_number": number}, headers=headers)

    return Starlette(routes=[
        Route("/", landing),
        Route("/cas", landing),
        Route("/health", health),
        Route("/cas/login", login, methods=["GET", "POST"]),
        Route("/cas/logout", logout, methods=["GET", "POST"]),
        Route("/cas/login.css", stylesheet),
        Route("/cas/privacy", privacy),
        Route("/cas/password.js", password_script),
        Route("/cas/password.css", password_style),
        Route("/cas/serviceValidate", validate),
        Route("/cas/p3/serviceValidate", validate),
        Route("/cas/internal/identities/{subject}", identity_status),
        *admin_routes(store, event_log),
    ])
