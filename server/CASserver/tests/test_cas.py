import asyncio
import re
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import httpx2

from server.CASserver.app import CALLBACK, CAS_NAMESPACE, create_app
from server.CASserver.store import CASAccountExists, CASStore, TICKET_SECONDS


ORIGIN = "http://127.0.0.1:8082"
SERVICE = ORIGIN + CALLBACK + "?state=st_" + "a" * 43
OTHER_SERVICE = ORIGIN + CALLBACK + "?state=st_" + "b" * 43
PASSWORD = "test-local1"
TEST_ADMIN_PASSWORD = "local-fixture-admin9"
NUMBER = "00123456"
NS = {"cas": CAS_NAMESPACE}


class CASProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory(dir=Path(__file__).resolve().parents[3])
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = CASStore(self.directory)
        self.store.add(NUMBER, PASSWORD)
        self.now = 1000
        self.app = create_app(self.directory, ORIGIN, clock=lambda: self.now)
        self.client = await self.enterAsyncContext(httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url=ORIGIN,
            headers={"x-cas-site-origin": ORIGIN, "origin": ORIGIN},
        ))

    async def open_login(self, *, service=SERVICE, renew=False):
        return await self.client.get("/cas/login", params={"service": service, "renew": str(renew).lower()})

    async def sign_in(self, *, renew=False, password=PASSWORD):
        page = await self.open_login(renew=renew)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.headers["referrer-policy"], "strict-origin")
        self.assertTrue(page.headers["content-security-policy"].endswith("form-action 'self'"))
        self.assertIn('<link rel="stylesheet" href="/cas/login.css">', page.text)
        self.assertIn('>学工号</label>', page.text)
        flow = re.search(r'name="lt" value="([^"]+)"', page.text)[1]
        return await self.client.post("/cas/login", data={"lt": flow, "username": NUMBER, "password": password})

    def ticket(self, response):
        self.assertEqual(response.status_code, 303, response.text)
        location = urlsplit(response.headers["location"])
        self.assertEqual(location.path, CALLBACK)
        return parse_qs(location.query)["ticket"][0]

    async def validate(self, ticket, *, service=SERVICE, renew=False, path="/cas/p3/serviceValidate"):
        response = await self.client.get(path, params={"service": service, "ticket": ticket, "renew": str(renew).lower()})
        self.assertEqual(response.status_code, 200)
        return ElementTree.fromstring(response.content)

    @staticmethod
    def error(root):
        return root.find("cas:authenticationFailure", NS).get("code")

    async def test_privacy_notice_does_not_require_login_or_create_a_session(self):
        page = await self.client.get("/cas/privacy")
        self.assertEqual(page.status_code, 200)
        self.assertIn("隐私说明", page.text)
        self.assertNotIn("set-cookie", page.headers)
        self.assertIn("form-action 'none'", page.headers["content-security-policy"])
        self.assertIn('href="/cas/privacy"', (await self.open_login()).text)
        script = await self.client.get("/cas/password.js")
        self.assertEqual(script.headers["content-type"].split(";", 1)[0], "text/javascript")

    async def test_replays_are_consumed_even_for_a_wrong_service(self):
        ticket = self.ticket(await self.sign_in())
        self.assertEqual(self.error(await self.validate(ticket, service=OTHER_SERVICE)), "INVALID_SERVICE")
        self.assertEqual(self.error(await self.validate(ticket)), "INVALID_TICKET")

        ticket = self.ticket(await self.open_login())
        responses = await asyncio.gather(self.validate(ticket), self.validate(ticket))
        self.assertEqual(sum(item.find("cas:authenticationSuccess", NS) is not None for item in responses), 1)

    async def test_renew_and_cas2_use_the_same_subject(self):
        first = await self.validate(self.ticket(await self.sign_in()), renew=True)
        subject = first.findtext("cas:authenticationSuccess/cas:user", namespaces=NS)
        self.assertRegex(subject, r"^[a-f0-9]{32}$")
        self.assertEqual(first.findtext(".//cas:campusNumber", namespaces=NS), NUMBER)
        self.assertEqual(self.error(await self.validate(self.ticket(await self.open_login()), renew=True)), "INVALID_TICKET")
        later = await self.validate(self.ticket(await self.sign_in(renew=True)), renew=True, path="/cas/serviceValidate")
        self.assertEqual(later.findtext("cas:authenticationSuccess/cas:user", namespaces=NS), subject)
        self.assertIsNone(later.find(".//cas:attributes", NS))

    async def test_login_restricts_service_and_requires_a_browser_bound_form(self):
        self.assertEqual((await self.open_login(service="https://external.example/" + SERVICE)).status_code, 400)
        page = await self.open_login()
        flow = re.search(r'name="lt" value="([^"]+)"', page.text)[1]
        data = {"lt": flow, "username": NUMBER, "password": PASSWORD}
        self.assertEqual((await self.client.post("/cas/login", data=data, headers={"origin": "https://elsewhere.example"})).status_code, 400)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),
                                      base_url=ORIGIN, headers=self.client.headers) as unbound:
            self.assertEqual((await unbound.post("/cas/login", data=data)).status_code, 400)
        self.ticket(await self.client.post("/cas/login", data=data))
        self.assertEqual((await self.client.post("/cas/login", data=data)).status_code, 400)

    async def test_expiry_logout_and_password_reset_invalidate_tickets(self):
        ticket = self.ticket(await self.sign_in())
        self.now += TICKET_SECONDS
        self.assertEqual(self.error(await self.validate(ticket)), "INVALID_TICKET")
        ticket = self.ticket(await self.open_login())
        self.assertEqual((await self.client.post("/cas/logout")).status_code, 303)
        self.assertEqual(self.error(await self.validate(ticket)), "INVALID_TICKET")
        self.assertEqual((await self.open_login()).status_code, 200)
        ticket = self.ticket(await self.sign_in())
        self.store.change_password(NUMBER, "new-local1")
        self.assertEqual(self.error(await self.validate(ticket)), "INVALID_TICKET")
        self.assertEqual((await self.sign_in(password=PASSWORD)).status_code, 401)
        self.ticket(await self.sign_in(password="new-local1"))
        self.store.set_enabled(NUMBER, False)
        self.assertEqual((await self.sign_in(password="new-local1")).status_code, 401)

    async def test_published_origins_accept_only_their_own_website_callback(self):
        sites = {
            "https://192.168.42.10:8443": "https://192.168.42.10:8444",
            "https://agent.example.com:28704": "https://cas.example.com:25379",
        }
        app = create_app(self.directory, ORIGIN, clock=lambda: self.now, public_sites=sites)
        for website, cas in sites.items():
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url=cas,
                headers={"x-cas-site-origin": cas, "origin": cas},
            ) as browser:
                service = website + CALLBACK + "?state=st_" + "a" * 43
                wrong = next(origin for origin in sites if origin != website)
                rejected = await browser.get("/cas/login", params={"service": service.replace(website, wrong)})
                self.assertEqual(rejected.status_code, 400)
                page = await browser.get("/cas/login", params={"service": service})
                self.assertEqual(page.status_code, 200, page.text)
                self.assertIn("Secure", page.headers["set-cookie"])
                form_action = page.headers["content-security-policy"].split("; ")[-1]
                self.assertEqual(form_action, f"form-action 'self' {website}{CALLBACK}")
                flow = re.search(r'name="lt" value="([^"]+)"', page.text)[1]
                response = await browser.post("/cas/login", data={"lt": flow, "username": NUMBER, "password": PASSWORD})
                self.assertTrue(response.headers["location"].startswith(service + "&ticket="))
                ticket = self.ticket(response)
                self.assertIsNotNone((await self.validate(ticket, service=service)).find("cas:authenticationSuccess", NS))

    async def test_independent_administrator_and_password_rotation(self):
        origin = "http://127.0.0.1:8193"
        data = {"username": "admin", "password": TEST_ADMIN_PASSWORD}
        transport = httpx2.ASGITransport(app=self.app)
        async with httpx2.AsyncClient(transport=transport, base_url=origin, headers={"origin": origin}) as browser:
            self.assertEqual((await browser.get("/admin", headers={"host": "cas.example.com"})).status_code, 404)
            denied = await browser.post("/admin/login", data=data, headers={"origin": "https://cas.example.com"})
            self.assertEqual(denied.status_code, 403)
            page = await browser.get("/admin")
            self.assertNotIn(NUMBER, page.text)
            self.assertEqual((await browser.post("/admin/login", data=data)).status_code, 401)
            self.store.change_administrator_password(TEST_ADMIN_PASSWORD)
            signed_in = await browser.post("/admin/login", data=data)
            self.assertEqual(signed_in.status_code, 303)
            self.assertIn("HttpOnly", signed_in.headers["set-cookie"])
            self.assertIn(NUMBER, (await browser.get("/admin")).text)
            self.assertIsNone(self.store.authenticate("admin", data["password"], SERVICE, ""))
            self.store.change_administrator_password("local-rotation9")
            self.assertNotIn(NUMBER, (await browser.get("/admin")).text)
        recreated = create_app(self.directory, ORIGIN, clock=lambda: self.now)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=recreated),
                                      base_url=origin, headers={"origin": origin}) as browser:
            self.assertEqual((await browser.post("/admin/login", data=data)).status_code, 401)
            data["password"] = "local-rotation9"
            self.assertEqual((await browser.post("/admin/login", data=data)).status_code, 303)
        transport = httpx2.ASGITransport(app=recreated, client=("192.168.42.20", 12345))
        async with httpx2.AsyncClient(transport=transport, base_url=origin) as browser:
            self.assertEqual((await browser.get("/admin")).status_code, 404)

    async def test_administrator_batch_is_atomic_and_campus_password_reset_revokes_tickets(self):
        origin = "http://127.0.0.1:8193"
        self.store.change_administrator_password(TEST_ADMIN_PASSWORD)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), base_url=origin,
                                      headers={"origin": origin}) as browser:
            data = {"username": "admin", "password": TEST_ADMIN_PASSWORD}
            self.assertEqual((await browser.post("/admin/login", data=data)).status_code, 303)
            headers = {"content-type": "application/octet-stream"}
            path = "/admin/import?format=csv"
            duplicate = f"学工号,密码\n001,example01\n{NUMBER},example02\n".encode()
            response = await browser.post(path, content=duplicate, headers=headers)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("第 3 行", response.json()["message"])
            self.assertEqual([row["username"] for row in self.store.accounts()], [NUMBER])
            valid = b"campus_number,password\n001,example01\n002,example02\n"
            response = await browser.post(path, content=valid, headers=headers)
            self.assertEqual((response.status_code, response.json()), (201, {"created": 2}))
            ticket = self.ticket(await self.sign_in())
            response = await browser.post("/admin/accounts", data={
                "action": "password", "username": NUMBER,
                "password": "local-updated9", "confirmation": "local-updated9",
            })
            self.assertEqual(response.status_code, 303)
            self.assertEqual(self.error(await self.validate(ticket)), "INVALID_TICKET")

    async def test_admin_creation_form_and_live_log_preserve_secrets(self):
        origin = "http://127.0.0.1:8193"
        self.store.change_administrator_password(TEST_ADMIN_PASSWORD)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), base_url=origin,
                                      headers={"origin": origin}) as browser:
            self.assertEqual((await browser.get("/admin/logs")).status_code, 401)
            self.assertEqual((await browser.post("/admin/login", data={"username": "admin", "password": TEST_ADMIN_PASSWORD})).status_code, 303)
            page = await browser.get("/admin")
            create_form = re.search(r'<form\b[^>]*data-password-form>(.*?)</form>', page.text, re.DOTALL)
            self.assertIsNotNone(create_form)
            self.assertEqual(create_form[1].count('name="action"'), 1)
            form = {"action": "add", "username": "000077", "password": "fixture-pass9", "confirmation": "different1"}
            response = await browser.post("/admin/accounts", data=form)
            self.assertEqual(response.status_code, 422)
            self.assertIn("两次输入的密码不一致", response.text)
            self.assertIn('value="000077"', response.text)
            self.assertNotIn(form["password"], response.text)
            form["confirmation"] = form["password"]
            with patch.object(CASStore, "add", side_effect=sqlite3.OperationalError("private fixture details")):
                self.assertEqual((await browser.post("/admin/accounts", data=form)).status_code, 503)
            created = await browser.post("/admin/accounts", data=form)
            self.assertEqual((created.status_code, created.headers["location"]), (303, "/admin?updated=add"))
            self.assertEqual((await browser.post("/admin/accounts", data=form)).status_code, 409)
            page = await browser.get("/admin?q=000077&edit=000077")
            self.assertIn("编辑账号 <bdi>000077", page.text)
            self.assertNotIn(NUMBER, page.text)
            reply = await browser.get("/admin/logs")
            self.assertEqual(reply.headers["cache-control"], "no-store")
            self.assertNotIn("private fixture details", reply.text)
            self.assertNotIn(form["password"], reply.text)
            self.assertNotIn(form["username"], reply.text)
            messages = {row["message"] for row in reply.json()["items"]}
            self.assertTrue({"账号已创建。", "创建账号失败（学工号已存在）。", "创建账号失败（数据库写入失败）。"} <= messages)
            self.assertEqual((await browser.get("/admin/logs", headers={"host": "cas.example.com"})).status_code, 404)


class CASUpgradeTests(unittest.TestCase):
    def test_legacy_alias_needs_a_number_and_keeps_its_bound_subject(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parents[3]) as temporary:
            directory = Path(temporary)
            original = CASStore(directory)
            original.add("0008", PASSWORD)
            original.add(NUMBER, PASSWORD)
            ticket, session = original.authenticate(NUMBER, PASSWORD, SERVICE, "")
            with closing(sqlite3.connect(original.path)) as conn, conn:
                subject = conn.execute("SELECT subject FROM accounts WHERE username=?", (NUMBER,)).fetchone()[0]
                conn.execute("UPDATE accounts SET username='legacy-01' WHERE subject=?", (subject,))
                conn.execute("ALTER TABLE accounts DROP COLUMN is_campus_number")

            store = CASStore(directory)
            self.assertEqual([(row["username"], row["is_campus_number"]) for row in store.accounts()],
                             [("0008", 1), ("legacy-01", 0)])
            self.assertIsNone(store.authenticate("legacy-01", PASSWORD, SERVICE, ""))
            self.assertEqual(store.validate(ticket, SERVICE, False), (None, "INVALID_TICKET"))
            self.assertIsNone(store.start_login(SERVICE, False, session, "local")[0])
            self.assertIsNone(store.active_campus_number(subject))

            with self.assertRaises(CASAccountExists):
                store.set_campus_number("legacy-01", "0008")
            self.assertEqual(store.set_campus_number("legacy-01", "Staff-0007"), "staff-0007")
            ticket, _ = store.authenticate("staff-0007", PASSWORD, SERVICE, "")
            account, error = store.validate(ticket, SERVICE, True)
            self.assertIsNone(error)
            self.assertEqual((account["subject"], store.active_campus_number(subject)), (subject, "staff-0007"))
