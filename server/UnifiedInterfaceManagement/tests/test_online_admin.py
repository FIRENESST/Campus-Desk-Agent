import asyncio
import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx2

from gateway import create_app
from MainProcessManager.online_admin_store import IDLE_SECONDS, OnlineAdminStore
from server.CASserver.store import CASStore, STATE_SUBDIR
from settings import Settings


BRIDGE = "b" * 43
ORIGIN = "http://127.0.0.1:8082"
SITE_SESSION = "a" * 64
USER_ID = "749cb25e-423a-4142-935c-5b58b7407ddc"
GUEST_ID = "094726bb-4e8a-4b9c-93c5-2ea838639ef0"
EMAIL = "operator@example.com"


class Workers:
    def __init__(self):
        self.calls = []

    async def call(self, service, action, arguments):
        self.calls.append((service, action, arguments))
        return 200, {"items": []}


class OnlineAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory(dir=Path(__file__).resolve().parents[3])
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        state = self.directory / "online-admin"
        self.store = OnlineAdminStore(state)
        self.store.grant("owner", USER_ID, EMAIL)
        self.identities = {
            "Bearer ac_operator": {"id": USER_ID, "email": EMAIL, "verified_at": "2026-01-01T00:00:00Z", "campus_status": "ready"},
            "Bearer ac_guest": {"id": GUEST_ID, "email": "guest@example.com", "verified_at": "2026-01-01T00:00:00Z", "campus_status": "ready"},
        }
        self.workers = Workers()
        self.origin = ORIGIN
        self.config = Settings(mpm_auth=(r"\\.\pipe\mpm-auth-" + "a" * 32, 11), web_admin_bridge=BRIDGE,
                               web_admin_state=state, admin_token="r" * 43, runtime_status_key="s" * 43,
                               campus_rumor_key="t" * 43)
        self.enterContext(patch("gateway.authorize_request", return_value=True))
        self.app = create_app(self.config, workers=self.workers, transport=httpx2.MockTransport(self.identify))
        await self.enterAsyncContext(self.app.router.lifespan_context(self.app))
        self.client = await self.enterAsyncContext(httpx2.AsyncClient(
            base_url="http://127.0.0.1:8090", transport=httpx2.ASGITransport(app=self.app, client=("127.0.0.1", 31111)),
            headers={"x-campus-admin-bridge": BRIDGE, "x-campus-admin-origin": self.origin,
                     "x-campus-admin-ip": "127.0.0.1", "origin": self.origin,
                     "x-campus-admin-site-session": SITE_SESSION, "Authorization": "Bearer ac_operator"},
        ))

    def identify(self, request):
        if request.url.port == self.config.campus_rumor_port:
            self.assertEqual(request.headers["x-campus-rumor-key"], self.config.campus_rumor_key)
            self.assertEqual(request.headers["x-campus-rumor-audience"], "admin")
            self.assertEqual(request.headers["x-campus-rumor-actor"], "campus-desk:" + USER_ID)
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("authorization", request.headers)
            if request.url.path == "/admin":
                nonce = request.headers["x-campus-rumor-nonce"]
                return httpx2.Response(200, headers={"content-type": "text/html; charset=utf-8", "set-cookie": "legacy=unsafe"},
                                       text=f'<script nonce="{nonce}" src="/static/site.js"></script><a href="/posts">Posts</a>')
            self.assertEqual((request.method, request.url.path, request.headers.get("x-campus-request")),
                             ("POST", "/api/tips/1/status", "1"))
            return httpx2.Response(200, json={"ok": True})
        if request.url.path.startswith("/internal/weread/"):
            self.assertEqual((request.url.host, request.url.port), ("127.0.0.1", self.config.campus_digest_port))
            self.assertEqual(request.headers["x-campus-runtime-key"], self.config.runtime_status_key)
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("authorization", request.headers)
            return httpx2.Response(202 if request.method == "POST" else 200, json={
                "session": "needs_login", "phase": "waiting_scan",
                "qr_png_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode(),
                "confirm_url": "https://example.test/private", "log": ["hidden"],
            })
        self.assertEqual((request.method, request.url.path), ("GET", "/v1/me"))
        self.assertEqual(request.headers["x-forwarded-for"], "127.0.0.1")
        self.assertNotIn("cookie", request.headers)
        identity = self.identities.get(request.headers["authorization"])
        return httpx2.Response(200, json=identity) if identity else httpx2.Response(401)

    async def login(self, **headers):
        return await self.client.post("/admin/api/login", headers=headers)

    async def test_rumor_management_uses_the_verified_admin_session_and_audits_writes(self):
        path = "/admin/rumor"
        self.assertEqual((await self.client.get(path + "/admin")).status_code, 401)
        self.assertEqual((await self.login()).status_code, 200)
        page = await self.client.get(path + "/admin")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/admin/rumor/posts"', page.text)
        self.assertIn('src="/admin/rumor/static/site.js"', page.text)
        self.assertNotIn("set-cookie", page.headers)
        self.assertRegex(page.headers["content-security-policy"], r"'nonce-[A-Za-z0-9_-]{24}'")
        self.assertEqual((await self.client.get(path + "/internal/campus-desk/rumors")).status_code, 404)
        self.assertEqual((await self.client.get(path + "/admin", headers={"Authorization": "Bearer ac_guest"})).status_code, 401)
        review = path + "/api/tips/1/status"
        self.assertEqual((await self.client.post(review, json={"status": "reviewed"})).status_code, 403)
        self.assertEqual((await self.client.post(review, json={"status": "reviewed"}, headers={
            "X-Campus-Request": "1", "Origin": "https://different.example",
        })).status_code, 403)
        response = await self.client.post(review, json={"status": "reviewed"}, headers={"X-Campus-Request": "1"})
        self.assertEqual(response.json(), {"ok": True})
        events = [event for event in self.store.audit() if event["target"] == review]
        self.assertEqual([(event["action"], event["outcome"]) for event in events], [("POST", "completed")])

    async def test_weread_status_is_passive_and_scan_requires_admin_csrf(self):
        path = "/admin/api/digest/weread"
        self.assertEqual((await self.client.get(path)).status_code, 401)
        login = await self.login()
        csrf = {"X-Campus-Admin-CSRF": login.json()["csrf"]}
        self.assertEqual((await self.client.post(path + "/login")).status_code, 403)
        started = await self.client.post(path + "/login", headers=csrf)
        self.assertEqual(started.status_code, 202)
        self.assertEqual(set(started.json()), {"session", "phase", "qr_png_base64"})
        with patch("online_admin.OnlineAdminStore._transaction", side_effect=AssertionError("passive polling wrote to state")):
            polled = await self.client.get(path, headers=csrf)
            self.assertEqual(polled.json(), {"session": "needs_login", "phase": "waiting_scan"})
            scan = await self.client.get(path + "/login", headers=csrf)
            self.assertEqual(set(scan.json()), {"session", "phase", "qr_png_base64"})
        events = [event for event in self.store.audit() if event["target"].startswith(path)]
        self.assertEqual([(event["action"], event["outcome"]) for event in events], [("POST", "accepted")])

    async def test_session_is_separate_from_normal_and_service_credentials(self):
        denied = await self.client.get("/admin/api/data/users", headers={"Authorization": "Bearer " + "r" * 43,
                                                                        "Cookie": "campus-desk-dev=ordinary"})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual((await self.login(**{"x-campus-admin-bridge": "z" * 43})).status_code, 403)
        self.assertEqual((await self.login(origin="https://different.example")).status_code, 403)
        self.assertEqual((await self.client.post("/admin/api/login", json={"username": "admin", "password": "example"})).status_code, 413)
        login = await self.login()
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual((login.json()["name"], login.json()["user_id"]), (EMAIL, USER_ID))
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        self.assertIn("SameSite=strict", login.headers["set-cookie"])
        csrf = login.json()["csrf"]
        headers = {"X-Campus-Admin-CSRF": csrf}
        self.assertEqual((await self.client.get("/admin/api/data/users")).status_code, 403)
        self.assertEqual((await self.client.get("/admin/api/data/users?q=sample", headers=headers)).status_code, 200)
        self.assertEqual(self.workers.calls[0][0:2], ("postgres_worker", "list"))
        self.assertEqual(self.workers.calls[0][2]["actor"], "web:" + USER_ID)
        self.assertEqual((await self.client.get("/admin/api/data/users", headers={
            **headers, "x-campus-admin-origin": "http://127.0.0.1:8083",
        })).status_code, 401)
        for changed in ({"x-campus-admin-site-session": "c" * 64}, {"authorization": "Bearer ac_guest"}):
            self.assertEqual((await self.client.get("/admin/api/session", headers=changed)).status_code, 401)
        self.assertEqual((await self.client.post("/admin/api/minta/v1/call", headers=headers, json={})).status_code, 404)
        self.assertEqual((await self.client.post("/admin/api/logout", headers=headers)).status_code, 204)
        self.assertEqual((await self.client.get("/admin/api/data/users", headers=headers)).status_code, 401)

    async def test_verified_email_and_current_uuid_grant_are_required(self):
        denied = await self.login(authorization="Bearer ac_guest")
        self.assertEqual((denied.status_code, denied.json()["error"]["code"]), (403, "admin_permission_required"))
        self.assertNotIn("set-cookie", denied.headers)
        self.identities["Bearer ac_operator"]["verified_at"] = None
        unverified = await self.login()
        self.assertEqual((unverified.status_code, unverified.json()["error"]["code"]), (403, "admin_email_unverified"))

    async def test_granted_user_must_complete_campus_onboarding(self):
        self.identities["Bearer ac_operator"]["campus_status"] = "profile_required"
        denied = await self.login()
        self.assertEqual((denied.status_code, denied.json()["error"]["code"]), (403, "admin_campus_required"))
        self.assertNotIn("set-cookie", denied.headers)

    async def test_campus_admin_writes_require_csrf_and_use_the_verified_actor(self):
        login = await self.login()
        headers = {"X-Campus-Admin-CSRF": login.json()["csrf"]}
        rule = {"blocked": True, "audience": {"allow_grades": ["ug2"]}}
        room = "/admin/api/campus/rules/classroom/room-a101"
        self.assertEqual((await self.client.put(room, json=rule)).status_code, 403)
        self.assertEqual((await self.client.put(room, headers=headers, json=rule)).status_code, 200)
        service, action, arguments = self.workers.calls[-1]
        self.assertEqual((service, action, arguments["actor"], arguments["id"], arguments["payload"]),
                         ("postgres_worker", "campus_rule_update", "web:" + USER_ID, "room-a101", rule))
        review = {"version": 1, "status": "approved"}
        response = await self.client.patch("/admin/api/campus/clubs/review/" + USER_ID, headers=headers, json=review)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.workers.calls[-1][1], "club_review")

    async def test_routing_feedback_management_uses_the_online_admin_session(self):
        routing = "/admin/api/routing"
        sample = "e" * 32
        self.assertEqual((await self.client.get(routing + "/overview")).status_code, 401)
        login = await self.login()
        headers = {"X-Campus-Admin-CSRF": login.json()["csrf"]}
        self.assertEqual((await self.client.get(routing + "/overview?days=7", headers=headers)).status_code, 200)
        self.assertEqual(self.workers.calls[-1], ("routing_worker", "overview", {"days": 7}))
        self.assertEqual((await self.client.get(routing + "/overview?days=7&days=14", headers=headers)).status_code, 422)
        page = await self.client.get(
            routing + "/samples?status=different&agent=campus_services&offset=25&limit=25", headers=headers,
        )
        self.assertEqual(page.status_code, 200)
        self.assertEqual(self.workers.calls[-1], ("routing_worker", "list", {
            "limit": 25, "offset": 25, "status": "different", "agent": "campus_services",
            "origin": "", "q": "", "day": "",
        }))
        actions = [
            ("POST", "/preview", {"prompt": "学校体育馆如何预约"}, "preview", {"prompt": "学校体育馆如何预约"}),
            ("PUT", f"/samples/{sample}/grade", {"score": 3, "targets": [], "direct": True}, "grade",
             {"sample_id": sample, "score": 3, "targets": [], "direct": True}),
            ("DELETE", f"/samples/{sample}/grade", None, "clear_grade", {"sample_id": sample}),
            ("DELETE", "/introductions/campus_services", None, "reset_introduction", {"role": "campus_services"}),
            ("POST", "/samples/bulk-delete", {"sample_ids": [sample]}, "bulk_delete", {"sample_ids": [sample]}),
        ]
        for method, path, payload, target, arguments in actions:
            with self.subTest(path=path):
                self.assertEqual((await self.client.request(method, routing + path, json=payload)).status_code, 403)
                response = await self.client.request(method, routing + path, headers=headers, json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.workers.calls[-1], ("routing_worker", target, arguments))
        denied = await self.client.post(routing + "/samples/bulk-delete", headers=headers, json={"sample_ids": ["invalid"]})
        self.assertEqual(denied.status_code, 422)
        invalid_grade = await self.client.put(
            routing + f"/samples/{sample}/grade", headers=headers,
            json={"score": 3, "targets": ["campus_services"], "direct": True},
        )
        self.assertEqual(invalid_grade.status_code, 422)

    async def test_campus_photo_review_and_preview_require_admin_session_and_csrf(self):
        endpoint = "/admin/api/campus/photos/review"
        preview = endpoint + "/" + USER_ID + "/image"
        delete = endpoint + "/" + USER_ID
        places = "/admin/api/campus/photos/places"
        self.assertEqual((await self.client.get(preview)).status_code, 401)
        self.assertEqual((await self.client.delete(delete + "?version=2")).status_code, 401)
        login = await self.login()
        headers = {"X-Campus-Admin-CSRF": login.json()["csrf"]}
        self.assertEqual((await self.client.get(preview)).status_code, 403)
        self.assertEqual((await self.client.get(endpoint + "?status=pending&offset=20", headers=headers)).status_code, 200)
        self.assertEqual(self.workers.calls[-1], ("postgres_worker", "photo_reviews", {
            "actor": "web:" + USER_ID, "id": None, "status": "pending", "offset": 20,
        }))
        self.assertEqual((await self.client.get(endpoint + "?status=anything", headers=headers)).status_code, 422)
        self.assertEqual((await self.client.get(places + "?status=all", headers=headers)).status_code, 200)
        self.assertEqual(self.workers.calls[-1], ("postgres_worker", "photo_review_places", {
            "actor": "web:" + USER_ID, "id": None, "status": "all", "offset": 0,
        }))
        self.assertEqual((await self.client.get(endpoint + "?place=1165519_399134", headers=headers)).status_code, 200)
        self.assertEqual(self.workers.calls[-1], ("postgres_worker", "photo_reviews", {
            "actor": "web:" + USER_ID, "id": None, "status": "pending", "offset": 0, "place": "1165519_399134",
        }))
        self.assertEqual((await self.client.get(endpoint + "?place=invalid", headers=headers)).status_code, 422)
        with patch.object(self.workers, "call", return_value=(200, {"image_base64": "/9j/2Q=="})) as worker:
            image = await self.client.get(preview, headers=headers)
            self.assertEqual((image.status_code, image.headers["content-type"], image.content),
                             (200, "image/jpeg", b"\xff\xd8\xff\xd9"))
            worker.assert_awaited_once_with("postgres_worker", "photo_preview", {"actor": "web:" + USER_ID, "id": USER_ID})
        update = {"version": 1, "status": "approved"}
        self.assertEqual((await self.client.patch(endpoint + "/" + USER_ID, json=update)).status_code, 403)
        self.assertEqual((await self.client.patch(endpoint + "/" + USER_ID, headers=headers, json=update)).status_code, 200)
        self.assertEqual(self.workers.calls[-1], ("postgres_worker", "photo_review", {
            "actor": "web:" + USER_ID, "id": USER_ID, "payload": update,
        }))
        self.assertEqual((await self.client.delete(delete + "?version=2")).status_code, 403)
        self.assertEqual((await self.client.delete(delete, headers=headers)).status_code, 422)
        self.assertEqual((await self.client.delete(delete + "?version=2147483648", headers=headers)).status_code, 422)
        self.assertEqual((await self.client.delete(delete + "?version=2", headers=headers)).status_code, 200)
        self.assertEqual(self.workers.calls[-1], ("postgres_worker", "photo_delete", {
            "actor": "web:" + USER_ID, "id": USER_ID, "version": 2,
        }))

    async def test_club_archive_management_requires_admin_session_and_csrf(self):
        archive = "/admin/api/campus/clubs/archives"
        payload = {"action": "purge", "version": 3}
        self.assertEqual((await self.client.get(archive)).status_code, 401)
        login = await self.login()
        headers = {"X-Campus-Admin-CSRF": login.json()["csrf"]}
        self.assertEqual((await self.client.get(archive + "?offset=40", headers=headers)).status_code, 200)
        self.assertEqual(self.workers.calls[-1][1:2], ("club_archives",))
        self.assertEqual(self.workers.calls[-1][2]["offset"], 40)
        target = archive + "/" + USER_ID
        self.assertEqual((await self.client.patch(target, json=payload)).status_code, 403)
        self.assertEqual((await self.client.patch(target, headers=headers, json=payload)).status_code, 200)
        service, action, arguments = self.workers.calls[-1]
        self.assertEqual((service, action, arguments["actor"], arguments["id"], arguments["payload"]),
                         ("postgres_worker", "club_archive", "web:" + USER_ID, USER_ID, payload))

    async def test_uuid_revocation_and_idle_expiry_end_elevated_sessions(self):
        login = await self.login()
        headers = {"X-Campus-Admin-CSRF": login.json()["csrf"]}
        store = self.app.state.gateway.online_admin.store
        start = int(store.clock())
        store.clock = lambda: start + IDLE_SECONDS + 1
        self.assertEqual((await self.client.get("/admin/api/data/users", headers=headers)).status_code, 401)
        login = await self.login()
        self.assertEqual(login.status_code, 200)
        self.assertTrue(self.store.revoke("owner", USER_ID))
        self.assertEqual((await self.client.get("/admin/api/data/users", headers={"X-Campus-Admin-CSRF": login.json()["csrf"]})).status_code, 401)
        self.assertEqual((await self.login()).status_code, 403)
        self.store.grant("owner", USER_ID, EMAIL)
        self.assertEqual((await self.client.get("/admin/api/session")).status_code, 401)

    async def test_invalid_email_login_is_rejected_before_a_management_request(self):
        login = await self.login()
        del self.identities["Bearer ac_operator"]
        response = await self.client.get("/admin/api/data/users", headers={"X-Campus-Admin-CSRF": login.json()["csrf"]})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (401, "admin_site_session_invalid"))
        self.assertEqual(self.workers.calls, [])

    async def test_only_authorized_web_actor_is_sent_to_supervisor(self):
        login = await self.login()
        headers = {"X-Campus-Admin-CSRF": login.json()["csrf"]}
        with patch("online_admin.runtime_request", return_value={"allowed": True, "job": {"id": "job-1"}}) as runtime:
            sent = await self.client.post("/admin/api/runtime/processes/sim-classroom", headers=headers, json={"action": "restart"})
            self.assertEqual(sent.status_code, 202, sent.text)
            self.assertEqual(runtime.call_args.args[-1], {"operation": "submit", "actor": USER_ID,
                                                        "service": "sim-classroom", "action": "restart"})
        with patch("online_admin.runtime_request", return_value={"allowed": False}):
            denied = await self.client.post("/admin/api/runtime/processes/unified-interface", headers=headers, json={"action": "stop"})
        self.assertEqual(denied.status_code, 403)
        actions = (await self.client.get("/admin/api/audit", headers=headers)).json()["items"]
        self.assertTrue(any(row["target"].endswith("/sim-classroom") and row["outcome"] == "accepted" for row in actions))

    async def test_agent_administrators_cannot_access_the_cas_account_directory(self):
        login = await self.login()
        self.assertEqual(login.status_code, 200, login.text)
        headers = {"X-Campus-Admin-CSRF": login.json()["csrf"]}
        endpoint = "/admin/api/cas"
        self.assertEqual((await self.client.get(endpoint + "/accounts", headers=headers)).status_code, 404)
        self.assertEqual((await self.client.post(endpoint + "/accounts/00012001", headers=headers, json={})).status_code, 404)
        self.assertEqual((await self.client.post(endpoint + "/import?format=csv", headers=headers)).status_code, 404)

    async def test_profile_batch_reuses_admin_authorization_and_live_cas_status(self):
        route = "/admin/api/cas/profiles"
        target = {"id": GUEST_ID, "revision": "c8df705c-e8d8-4f57-ae90-c9cf98863fd0"}
        payload = {"items": [target]}
        bulk_route = route + "/bulk-review"
        self.assertEqual((await self.client.post(bulk_route, json=payload)).status_code, 401)
        login = await self.login()
        headers = {"X-Campus-Admin-CSRF": login.json()["csrf"]}
        self.assertEqual((await self.client.post(bulk_route, json=payload)).status_code, 403)

        directory = self.directory / STATE_SUBDIR
        cas = CASStore(directory)
        await asyncio.to_thread(cas.add, "00012001", "cas-password8")
        ticket, _ = await asyncio.to_thread(cas.authenticate, "00012001", "cas-password8", "test", "")
        account, _ = cas.validate(ticket, "test", True)
        row = {**target, "issuer": "urn:campus-desk:cas:local", "subject": account["subject"], "protocol": "cas",
               "is_active": True, "email_verified": True, "campus_number": "00012001", "reviewed_at": None}

        with patch.object(self.workers, "call", side_effect=lambda *_: (200, {"items": [row.copy()]})):
            active = (await self.client.get(route, headers=headers)).json()["items"][0]
            self.assertTrue(active["batch_reviewable"])
            self.assertNotIn("subject", active)
            await asyncio.to_thread(cas.set_enabled, "00012001", False)
            disabled = (await self.client.get(route, headers=headers)).json()["items"][0]
            self.assertFalse(disabled["batch_reviewable"])

        self.assertEqual((await self.client.post(bulk_route, headers=headers, json=payload)).status_code, 200)
        service, action, arguments = self.workers.calls[-1]
        self.assertEqual((service, action), ("postgres_worker", "campus_profiles_bulk_review"))
        self.assertEqual((arguments["actor"], arguments["payload"], arguments["cas_directory"]),
                         ("web:" + USER_ID, payload, str(directory)))
